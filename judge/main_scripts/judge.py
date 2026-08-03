"""Run a local judge on specified attempt and solution files, using the prompt template and rubric."""

import json
import shutil
import string
import time
import traceback
import uuid
from pathlib import Path

from openai import OpenAI
from utils.excel_utils import (
    calculate_message_size_for_files,
    copy_support_files,
    find_golden_solution_file,
    prepare_directory_files,
    process_case_files,
    shorten_attempt_csv_files,
    shorten_solution_csv_files,
)
from utils.llm_utils import (
    calculate_cost,
    get_client,
    is_anthropic_model,
    is_openai_model,
    robust_send_message,
    strip_unsupported_anthropic_images,
    to_api_model_id,
)
from utils.logger import add_log_file, logger, remove_log_file
from utils.misc_utils import (
    dump_messages_yaml,
    get_absolute_path,
    load_env_var,
    load_project_configs,
    relative_path_from_project_root,
    str2bool,
)
from utils.prompt_utils import (
    add_file_confirmation,
    build_check_name_mapping,
    compile_prompt,
    encode_file_to_base64,
    format_file_section,
    render_rubric_checks,
)

### Obtain constants
load_project_configs()
JUDGE_MODEL = load_env_var("JUDGE_OPENROUTER_MODEL", required=True)
DEFAULT_SOLUTION_CONTEXT_CHAR_LIMIT = int(
    load_env_var("JUDGE_DEFAULT_SOLUTION_CONTEXT_CHAR_LIMIT", required=True)
)
DEFAULT_ATTEMPT_CONTEXT_CHAR_LIMIT = int(
    load_env_var("JUDGE_DEFAULT_ATTEMPT_CONTEXT_CHAR_LIMIT", required=True)
)
DEFAULT_TOTAL_CHARACTER_LIMIT = int(
    load_env_var("JUDGE_DEFAULT_TOTAL_CHARACTER_LIMIT", required=True)
)
RUBRIC_MAX_MISTAKES = int(
    load_env_var("JUDGE_RUBRIC_MAX_MISTAKES", default=1),
)
AGENTIC_JUDGE_CONTEXT_TOKEN_LIMIT = int(
    load_env_var("AGENTIC_JUDGE_CONTEXT_TOKEN_LIMIT", default=1_000_000),
)
AGENTIC_JUDGE_MAX_ROUNDS = int(
    load_env_var("AGENTIC_JUDGE_MAX_ROUNDS", default=50),
)
READ_FILE_MAX_CELLS = int(
    load_env_var("AGENTIC_JUDGE_READ_FILE_MAX_CELLS", default=5000),
)

### Custom Errors


class JudgeOutputError(Exception):
    """Raised when the judge model returns valid JSON but with an unexpected structure."""

    pass


import re

_CODE_FENCE_RE = re.compile(r"```(?:json)?\s*\n([\s\S]*?)```", re.IGNORECASE)


def _extract_json_from_response(text: str) -> str:
    """Extract JSON from a model response that may contain markdown fences or preamble text.

    Handles:
    - Raw JSON (no fences)
    - ```json ... ``` with or without text before/after
    - ``` ... ``` (no language tag)
    """
    stripped = text.strip()
    # Fast path: already valid-looking JSON. Some models (e.g.
    # gemini-3-flash-preview) append commentary AFTER the JSON value, which
    # makes a plain json.loads raise "Extra data" and burns the caller's
    # parse-retry budget. raw_decode parses only the first complete JSON
    # value and reports where it ends, so trailing text is dropped.
    if stripped.startswith("{") or stripped.startswith("["):
        try:
            _, end = json.JSONDecoder().raw_decode(stripped)
            return stripped[:end]
        except ValueError:
            return stripped  # let the caller's json.loads raise clearly

    # Look for a fenced code block anywhere in the response
    match = _CODE_FENCE_RE.search(stripped)
    if match:
        return match.group(1).strip()

    # Fallback: return as-is and let json.loads raise
    return stripped


class RubricWeightConsistencyError(Exception):
    """Raised when rubric and weight files are inconsistent."""

    pass


### Scoring functions
def validate_rubric_weights_consistency(rubric_path: str, weights_path: str) -> None:
    """Validate that the rubric and weights files are consistent.

    Checks:
    1. All categories in weights exist in rubric and vice versa.
    2. Check names within each category match between rubric and weights.
    3. CategoryWeights has all expected categories and sums to ~1.

    Raises:
        RubricWeightConsistencyError: If any inconsistency is found.
    """
    with open(rubric_path, "r", encoding="utf-8") as f:
        rubric = json.load(f)
    with open(weights_path, "r", encoding="utf-8") as f:
        weights = json.load(f)

    expected_categories = ["Accuracy", "Formula", "Formatting"]
    errors = []

    # Check CategoryWeights
    if "CategoryWeights" not in weights:
        errors.append("Weights file missing 'CategoryWeights' key.")
    else:
        cat_weights = weights["CategoryWeights"][0]
        for cat in expected_categories:
            if cat not in cat_weights:
                errors.append(f"CategoryWeights missing category: {cat}")
        weight_sum = sum(cat_weights.get(cat, 0) for cat in expected_categories)
        if not (0.99 <= weight_sum <= 1.01):
            errors.append(f"CategoryWeights must sum to 1, got {weight_sum:.4f}")

    # Check each category's checks match by name
    for cat in expected_categories:
        rubric_names = []
        if cat in rubric:
            if isinstance(rubric[cat], list):
                rubric_names = [item["name"] for item in rubric[cat] if "name" in item]
        else:
            errors.append(f"Category '{cat}' missing from rubric file.")

        weight_names = []
        if cat in weights:
            if isinstance(weights[cat], list):
                weight_names = [item["name"] for item in weights[cat] if "name" in item]
        else:
            errors.append(f"Category '{cat}' missing from weights file.")

        # Compare names
        rubric_set = set(rubric_names)
        weight_set = set(weight_names)
        in_rubric_only = rubric_set - weight_set
        in_weights_only = weight_set - rubric_set
        if in_rubric_only:
            errors.append(
                f"{cat}: checks in rubric but not in weights: {sorted(in_rubric_only)}"
            )
        if in_weights_only:
            errors.append(
                f"{cat}: checks in weights but not in rubric: {sorted(in_weights_only)}"
            )

    if errors:
        raise RubricWeightConsistencyError(
            "Rubric/weights inconsistency:\n  " + "\n  ".join(errors)
        )


def calculate_check_score(mistakes: int, max_mistakes: int = 5) -> float:
    """Calculate score for a single check, normalized to 0-1 range."""
    raw_score = max(0, max_mistakes - mistakes)
    return raw_score / max_mistakes


def calculate_scores(all_responses: dict, weights: dict, max_mistakes: int = 5) -> dict:
    """Calculate weighted scores from judgement results and weights.

    Matches checks between judgement and weights by the 'name' field.

    Conservative on silent-failure edges:
      - Checks present in weights but missing from judgement score 0 (and are
        recorded in scoring_warnings["unscored_checks"]), rather than defaulting
        to 0 mistakes / full credit.
      - An empty judgement list for a category is recorded in
        scoring_warnings["empty_category_judgements"]; combined with the rule
        above, every weighted check ends up unscored.
      - Duplicate check names keep the worst (max mistakes) rather than
        last-write-wins, and the incident is recorded in
        scoring_warnings["duplicate_judgements"].
      - Mistake counts are taken from len(mistakes); if the model also
        reported `total_mistakes` and it disagrees, the mismatch is recorded
        in scoring_warnings["mistake_count_mismatches"] but the structured
        list wins.

    Args:
        all_responses: Judgement results dict {category: [check_items...]}.
        weights: Weights dict with CategoryWeights and per-category check weights.
        max_mistakes: Maximum mistakes before score is 0 (default 5).

    Returns:
        Dictionary with check_scores, criteria_scores, total_score (0-100),
        and scoring_warnings.
    """
    results = {
        "check_scores": {},
        "criteria_scores": {},
        "total_score": 0.0,
        "scoring_warnings": {
            "unscored_checks": {},
            "empty_category_judgements": [],
            "duplicate_judgements": {},
            "mistake_count_mismatches": [],
            "fail_without_mistakes": [],
        },
    }
    scoring_warnings = results["scoring_warnings"]
    category_weights = weights["CategoryWeights"][0]

    total_score = 0.0

    for category in ["Accuracy", "Formula", "Formatting"]:
        category_data = all_responses.get(category, [])
        if not isinstance(category_data, list):
            logger.warning(
                f"  Skipping score for {category}: response is not a list (parse failure?). See category_data: {category_data}"
            )
            continue

        if not category_data:
            scoring_warnings["empty_category_judgements"].append(category)

        # Build name -> mistake count from judgement
        judgement_by_name = {}
        for item in category_data:
            name = item.get("name")
            if not name:
                logger.warning(
                    f"  Skipping item in {category} with missing name: {item}"
                )
                continue

            mistakes_list = item.get("mistakes", [])
            actual_mistakes = (
                len(mistakes_list) if isinstance(mistakes_list, list) else 0
            )
            if str(item.get("decision", "")).strip().lower() == "fail" and actual_mistakes == 0:
                actual_mistakes = max_mistakes
                scoring_warnings["fail_without_mistakes"].append(
                    {"category": category, "name": name}
                )
            if "total_mistakes" in item and item["total_mistakes"] != actual_mistakes:
                scoring_warnings["mistake_count_mismatches"].append(
                    {
                        "category": category,
                        "name": name,
                        "claimed_total_mistakes": item["total_mistakes"],
                        "actual_mistakes_len": actual_mistakes,
                    }
                )
            m = actual_mistakes

            if name in judgement_by_name:
                dupes = scoring_warnings["duplicate_judgements"].setdefault(
                    category, {}
                )
                dupes.setdefault(name, [judgement_by_name[name]]).append(m)
                judgement_by_name[name] = max(judgement_by_name[name], m)
            else:
                judgement_by_name[name] = m

        # Calculate scores for each check in weights
        category_check_scores = {}
        weighted_sum = 0.0
        total_weight = 0.0

        for check_weight in weights.get(category, []):
            check_name = check_weight["name"]
            weight = check_weight["weight"]

            if check_name in judgement_by_name:
                mistakes = judgement_by_name[check_name]
                unscored = False
            else:
                # Not evaluated by the judge — conservative penalty instead of
                # silently crediting as "0 mistakes".
                mistakes = max_mistakes
                unscored = True
                scoring_warnings["unscored_checks"].setdefault(category, []).append(
                    check_name
                )

            check_score = calculate_check_score(mistakes, max_mistakes)
            weighted_score = check_score * weight
            weighted_sum += weighted_score
            total_weight += weight

            category_check_scores[check_name] = {
                "mistakes": mistakes,
                "score": check_score,
                "weight": weight,
                "weighted_score": weighted_score,
                "unscored": unscored,
            }

        category_normalized_score = (
            weighted_sum / total_weight * 100 if total_weight > 0 else 0.0
        )

        cat_weight = category_weights.get(category, 0)
        results["check_scores"][category] = category_check_scores
        results["criteria_scores"][category] = {
            "weighted_sum": weighted_sum,
            "total_weight": total_weight,
            "normalized_score": category_normalized_score,
            "category_weight": cat_weight,
        }

        total_score += category_normalized_score * cat_weight

    results["total_score"] = total_score
    return results


### Main Judge Function


def _compute_effective_attempt_limit(
    solution_chars: int,
    attempt_char_limit: int,
    total_char_limit: int,
) -> int:
    """Compute the attempt-side char budget given how much solution already uses.

    If a positive total_char_limit is set and the solution leaves more room than
    the static attempt_char_limit, the attempt limit grows to fill the remainder.
    Otherwise the static attempt_char_limit applies.
    """
    effective = attempt_char_limit
    if total_char_limit and total_char_limit > 0:
        remaining_room = total_char_limit - solution_chars
        if remaining_room > attempt_char_limit:
            effective = remaining_room
    return effective


def judge_case(
    task_folder: str,
    client: OpenAI,
    rubric_path: str,
    template_path: str,
    rubric_weight_path: str = None,
    model: str = JUDGE_MODEL,
    no_file_check: bool = True,
    nocall: bool = False,
    noupload: bool = False,
    use_existing: bool = True,
    solution_context_char_limit: int = DEFAULT_SOLUTION_CONTEXT_CHAR_LIMIT,
    attempt_context_char_limit: int = DEFAULT_ATTEMPT_CONTEXT_CHAR_LIMIT,
    total_character_limit: int = DEFAULT_TOTAL_CHARACTER_LIMIT,
    attempt_model: str = None,
    run_calculation: bool = False,
    cached_solution_csv_dir: str = None,
    cached_attempt_csv_dir: str = None,
    attempt_sheet_name_filter: bool = False,
    ignore_sheets: list[str] | None = None,
    on_overflow: str = "route_to_agentic",
    agentic_template_path: str = None,
    carry_over_context: bool = True,
    max_tool_rounds: int = AGENTIC_JUDGE_MAX_ROUNDS,
    reasoning_effort: str | None = None,
):
    """Execute the complete judging workflow for a case using OpenRouter.

    Args:
        task_folder: Path to the task folder containing Excel files.
        client: Configured OpenRouter client.
        rubric_path: Path to the rubric JSON file.
        template_path: Path to the prompt template YAML file.
        model: Model identifier to use for API calls (the grader model).
        no_file_check: If True, skip file confirmation step (default: True).
            This should always be True. It's only kept optional for legacy reasons.
        nocall: If True, skip API calls (for testing).
        noupload: If True, skip file preparation (for testing).
        use_existing: If True, skip regenerating files if they already exist.
        solution_context_char_limit: Character limit for golden solution context.
        attempt_context_char_limit: Character limit for AI attempt context.
        total_character_limit: Total character limit for combined solution + attempt.
        attempt_model: Name of the AI model that generated the attempt being judged.
        run_calculation: If True, run Excel formula calculations before extracting CSVs.
        cached_solution_csv_dir: Path to a directory containing pre-extracted solution CSVs.
            When provided, skips solution xlsx CSV extraction and copies from this cache instead.
        cached_attempt_csv_dir: Path to a directory containing pre-extracted attempt CSVs.
            When provided, skips ai_attempt xlsx CSV extraction and copies from this cache instead.
        attempt_sheet_name_filter: If True, only keep attempt sheets starting with
            'answers_' or 'model_', stripping the prefix from the output name.
        on_overflow: What to do when the extracted CSVs exceed the char budget.
            "route_to_agentic" (default) hands off to ``agentic_judge_case`` with
            the unshortened CSVs as cached input. "shorten" preserves the legacy
            lossy CSV-shortening path.
        agentic_template_path: Required when ``on_overflow == "route_to_agentic"``
            and an overflow is actually triggered; supplies the prompt template
            for the agentic handoff. Must be set by callers that may overflow.
        carry_over_context: Forwarded to ``agentic_judge_case`` on auto-route only.
        max_tool_rounds: Forwarded to ``agentic_judge_case`` on auto-route only.

    Returns:
        dict: Dictionary with paths to ai_judgement.json and output_dir. When
        the run auto-routed to the agentic judge, ``result["auto_routed"]`` is
        True and the rest of the dict is whatever ``agentic_judge_case`` returns.
    """
    # Shared preparation: validation, file processing
    prep = _prepare_case(
        task_folder=task_folder,
        rubric_path=rubric_path,
        rubric_weight_path=rubric_weight_path,
        use_existing=use_existing,
        run_calculation=run_calculation,
        cached_solution_csv_dir=cached_solution_csv_dir,
        cached_attempt_csv_dir=cached_attempt_csv_dir,
        attempt_sheet_name_filter=attempt_sheet_name_filter,
        ignore_sheets=ignore_sheets,
    )

    cache_log_path = prep["cache_log_path"]
    task_folder_name = prep["task_folder_name"]
    output_dir = prep["output_dir"]
    golden_solution_stem = prep["golden_solution_stem"]
    golden_solution_dir = prep["golden_solution_dir"]
    ai_attempt_dir = prep["ai_attempt_dir"]
    context_file_path = prep["context_file_path"]
    weights_data = prep["weights_data"]
    rubric_json_path = prep["rubric_json_path"]
    start_time = prep["start_time"]
    versions = prep["versions"]
    CHECK_ORDER = prep["CHECK_ORDER"]

    logger.info("=" * 80)
    logger.info("OpenRouter Judge Evaluation Workflow")
    logger.info("=" * 80)
    logger.info(
        f"Grading task: {task_folder_name}, model: {model}, "
        f"prompt: {versions['PROMPT_VERSION']}, "
        f"rubric: {versions['RUBRIC_VERSION']}, "
        f"rubric weight version: {versions['RUBRIC_WEIGHT_VERSION']}, "
        f"judge version: {versions['JUDGE_VERSION']}"
    )
    logger.info("=" * 80)

    if noupload:
        logger.info("\n--noupload flag set. Skipping file preparation.")
        remove_log_file(cache_log_path)
        shutil.copy(cache_log_path, str(output_dir / "judge.log"))
        return

    # STEP 1.5: Decide whether to auto-route to the agentic judge.
    #
    # We measure raw extracted-CSV sizes BEFORE any shortening branch runs, so
    # the dirs we hand off (golden_solution_dir, ai_attempt_dir) are guaranteed
    # to be the original CSVs from process_case_files — NOT the lossy
    # *_shortened/ copies produced by the legacy shortening path further below.
    raw_solution_chars = (
        calculate_message_size_for_files(Path(golden_solution_dir))["total"]
        if golden_solution_dir and Path(golden_solution_dir).exists()
        else 0
    )
    raw_attempt_chars = (
        calculate_message_size_for_files(Path(ai_attempt_dir))["total"]
        if ai_attempt_dir and Path(ai_attempt_dir).exists()
        else 0
    )
    upfront_attempt_limit = _compute_effective_attempt_limit(
        raw_solution_chars, attempt_context_char_limit, total_character_limit
    )
    solution_over = (
        bool(solution_context_char_limit)
        and solution_context_char_limit > 0
        and raw_solution_chars > solution_context_char_limit
    )
    attempt_over = (
        bool(upfront_attempt_limit)
        and upfront_attempt_limit > 0
        and raw_attempt_chars > upfront_attempt_limit
    )

    if (solution_over or attempt_over) and on_overflow == "route_to_agentic":
        if agentic_template_path is None:
            raise ValueError(
                "judge_case: on_overflow='route_to_agentic' triggered but "
                "agentic_template_path was not provided. Caller must supply an "
                "agentic prompt template when auto-routing is enabled. "
                f"solution={raw_solution_chars:,} (limit {solution_context_char_limit:,}, "
                f"over={solution_over}); attempt={raw_attempt_chars:,} "
                f"(limit {upfront_attempt_limit:,}, over={attempt_over})."
            )
        sol_overflow = max(0, raw_solution_chars - solution_context_char_limit)
        att_overflow = max(0, raw_attempt_chars - upfront_attempt_limit)
        reasons = []
        if solution_over:
            reasons.append(
                f"solution exceeds limit by {sol_overflow:,} chars "
                f"({raw_solution_chars:,} > {solution_context_char_limit:,})"
            )
        if attempt_over:
            reasons.append(
                f"attempt exceeds limit by {att_overflow:,} chars "
                f"({raw_attempt_chars:,} > {upfront_attempt_limit:,})"
            )
        logger.info(
            "\n[Auto-route] Routing to agentic judge instead of shortening CSVs.\n"
            f"  Reason: {'; '.join(reasons)}.\n"
            f"  Character counts (raw, unshortened):\n"
            f"    solution: {raw_solution_chars:>12,} chars  "
            f"limit={solution_context_char_limit:>12,}  "
            f"{'OVER by ' + format(sol_overflow, ',') if solution_over else 'within'}\n"
            f"    attempt:  {raw_attempt_chars:>12,} chars  "
            f"limit={upfront_attempt_limit:>12,}  "
            f"{'OVER by ' + format(att_overflow, ',') if attempt_over else 'within'}\n"
            f"  Total budget: {total_character_limit:,} chars "
            f"(effective attempt limit derived from total - solution).\n"
            f"  Auto-route policy: on_overflow='route_to_agentic' "
            f"(set --on-overflow shorten to use the legacy lossy CSV-shortening path)."
        )
        # Detach the standard-judge log handler so the agentic call's _prepare_case
        # can attach its own without double-logging. The partial standard log
        # remains on disk under PATHS_SCRATCH_PATH/judge_cache/ for debugging.
        remove_log_file(cache_log_path)
        return agentic_judge_case(
            task_folder=task_folder,
            client=client,
            rubric_path=rubric_path,
            template_path=agentic_template_path,
            rubric_weight_path=rubric_weight_path,
            model=model,
            nocall=nocall,
            noupload=noupload,
            use_existing=use_existing,
            attempt_model=attempt_model,
            run_calculation=run_calculation,
            # Reuse the unshortened CSVs we just extracted. _prepare_case in the
            # agentic call will copytree from these dirs into its own output_dir.
            cached_solution_csv_dir=str(golden_solution_dir) if golden_solution_dir else None,
            cached_attempt_csv_dir=str(ai_attempt_dir) if ai_attempt_dir else None,
            attempt_sheet_name_filter=attempt_sheet_name_filter,
            carry_over_context=carry_over_context,
            max_tool_rounds=max_tool_rounds,
            reasoning_effort=reasoning_effort,
            auto_routed=True,
        )

    # STEP 2: Prepare files for OpenRouter
    logger.info("\n[Step 2] Preparing files for OpenRouter...")

    golden_solution_files = {}
    ai_attempt_files = {}

    # Check if golden solution needs shortening based on character count
    shortening_result = None
    solution_context_reduced = False
    attempt_context_reduced = False
    context_reduced_details = None
    effective_golden_solution_dir = golden_solution_dir
    final_solution_chars = 0
    if golden_solution_dir and Path(golden_solution_dir).exists():
        gs_dir = Path(golden_solution_dir)

        size_info = calculate_message_size_for_files(gs_dir)
        total_chars = size_info["total"]

        logger.info(
            f"\n[Step 2b] Golden solution size: {total_chars:,} chars "
            f"(limit: {solution_context_char_limit:,})"
        )

        if (
            solution_context_char_limit
            and solution_context_char_limit > 0
            and total_chars > solution_context_char_limit
        ):
            logger.info(
                f"  Exceeds limit by {total_chars - solution_context_char_limit:,} chars. "
                f"Applying shortening..."
            )

            shortened_dir = output_dir / f"{golden_solution_stem}_shortened"
            shortened_dir.mkdir(parents=True, exist_ok=True)

            for src_file in gs_dir.glob("*_full.csv"):
                shutil.copy(str(src_file), str(shortened_dir / src_file.name))
            for src_file in gs_dir.glob("*_additional_format.txt"):
                shutil.copy(str(src_file), str(shortened_dir / src_file.name))

            shortening_result = shorten_solution_csv_files(
                directory_path=shortened_dir,
                target_chars=solution_context_char_limit,
            )

            size_info_after = calculate_message_size_for_files(shortened_dir)
            total_chars_after = size_info_after["total"]
            per_file_after = size_info_after["per_file"]

            logger.info(
                f"  Shortened: {shortening_result['total_original']:,} -> "
                f"{shortening_result['total_shortened']:,} chars "
                f"(saved {shortening_result['total_original'] - shortening_result['total_shortened']:,}, "
                f"{shortening_result['steps_executed']} steps)"
            )
            logger.info("  Per-file character counts after shortening:")
            max_fname_len = max(len(fname) for fname in per_file_after.keys())
            for fname, fchars in per_file_after.items():
                logger.info(
                    f"    {fname:<{max_fname_len}}: {fchars['chars']:>12,} chars"
                )

            solution_context_reduced = True

            def _format_summary_entry(old_chars: int, new_chars: int) -> str:
                if old_chars == new_chars:
                    return f"{old_chars} (no change)"
                return f"{old_chars}->{new_chars}"

            context_reduced_details = {
                "summary": {
                    "solution": {
                        fname: _format_summary_entry(
                            size_info["per_file"].get(fname, {}).get("chars", 0),
                            finfo["chars"],
                        )
                        for fname, finfo in per_file_after.items()
                    }
                },
                "solution": {
                    "before": {
                        "total_chars": size_info["total"],
                        "per_file": size_info["per_file"],
                    },
                    "after": {
                        "total_chars": total_chars_after,
                        "per_file": per_file_after,
                    },
                    "shortening_info": {
                        "steps_executed": shortening_result["steps_executed"],
                        "chars_saved": shortening_result["total_original"]
                        - shortening_result["total_shortened"],
                    },
                },
            }

            context_reduction_path = output_dir / "_context_reduction.json"
            with open(context_reduction_path, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "solution_context_reduced": solution_context_reduced,
                        "attempt_context_reduced": attempt_context_reduced,
                        "context_reduced_details": context_reduced_details,
                    },
                    f,
                    indent=2,
                )
            logger.info(f"  Context reduction info saved to: {context_reduction_path}")

            effective_golden_solution_dir = str(shortened_dir)
            final_solution_chars = total_chars_after
        else:
            logger.info("  Within limit, no shortening needed.")
            final_solution_chars = total_chars

        golden_solution_files = prepare_directory_files(effective_golden_solution_dir)

    # Check if AI attempt needs shortening based on character count
    effective_ai_attempt_dir = ai_attempt_dir
    if ai_attempt_dir and Path(ai_attempt_dir).exists():
        ai_dir = Path(ai_attempt_dir)

        ai_size_info = calculate_message_size_for_files(ai_dir)
        ai_total_chars = ai_size_info["total"]

        # Calculate effective attempt limit dynamically
        effective_attempt_limit = _compute_effective_attempt_limit(
            final_solution_chars, attempt_context_char_limit, total_character_limit
        )
        if effective_attempt_limit != attempt_context_char_limit:
            logger.info(
                f"\n[Step 2c] Dynamic attempt limit: solution used {final_solution_chars:,} chars, "
                f"remaining room from total limit ({total_character_limit:,}) = "
                f"{total_character_limit - final_solution_chars:,} chars"
            )
            logger.info(
                f"  Effective attempt limit increased: {attempt_context_char_limit:,} -> "
                f"{effective_attempt_limit:,} chars"
            )

        logger.info(
            f"\n[Step 2c] AI attempt size: {ai_total_chars:,} chars "
            f"(limit: {effective_attempt_limit:,})"
        )

        if (
            effective_attempt_limit
            and effective_attempt_limit > 0
            and ai_total_chars > effective_attempt_limit
        ):
            logger.info(
                f"  Exceeds limit by {ai_total_chars - effective_attempt_limit:,} chars. "
                f"Applying shortening..."
            )

            ai_shortened_dir = output_dir / "ai_attempt_shortened"
            ai_shortened_dir.mkdir(parents=True, exist_ok=True)

            for src_file in ai_dir.glob("*_full.csv"):
                shutil.copy(str(src_file), str(ai_shortened_dir / src_file.name))
            for src_file in ai_dir.glob("*_additional_format.txt"):
                shutil.copy(str(src_file), str(ai_shortened_dir / src_file.name))

            ai_shortening_result = shorten_attempt_csv_files(
                directory_path=ai_shortened_dir,
                target_chars=effective_attempt_limit,
            )

            ai_size_info_after = calculate_message_size_for_files(ai_shortened_dir)
            ai_total_chars_after = ai_size_info_after["total"]
            ai_per_file_after = ai_size_info_after["per_file"]

            logger.info(
                f"  Shortened: {ai_shortening_result['total_original']:,} -> "
                f"{ai_shortening_result['total_shortened']:,} chars "
                f"(saved {ai_shortening_result['total_original'] - ai_shortening_result['total_shortened']:,}, "
                f"{ai_shortening_result['steps_executed']} steps)"
            )
            logger.info("  Per-file character counts after shortening:")
            if ai_per_file_after:
                ai_max_fname_len = max(len(fname) for fname in ai_per_file_after.keys())
                for fname, fchars in ai_per_file_after.items():
                    logger.info(
                        f"    {fname:<{ai_max_fname_len}}: {fchars['chars']:>12,} chars"
                    )

            def _format_summary_entry(old_chars: int, new_chars: int) -> str:
                if old_chars == new_chars:
                    return f"{old_chars} (no change)"
                return f"{old_chars}->{new_chars}"

            if not context_reduced_details:
                context_reduced_details = {"summary": {}}
            if "summary" not in context_reduced_details:
                context_reduced_details["summary"] = {}
            attempt_context_reduced = True
            context_reduced_details["summary"]["attempt"] = {
                fname: _format_summary_entry(
                    ai_size_info["per_file"].get(fname, {}).get("chars", 0),
                    finfo["chars"],
                )
                for fname, finfo in ai_per_file_after.items()
            }
            context_reduced_details["attempt"] = {
                "before": {
                    "total_chars": ai_size_info["total"],
                    "per_file": ai_size_info["per_file"],
                },
                "after": {
                    "total_chars": ai_total_chars_after,
                    "per_file": ai_per_file_after,
                },
                "shortening_info": {
                    "steps_executed": ai_shortening_result["steps_executed"],
                    "chars_saved": ai_shortening_result["total_original"]
                    - ai_shortening_result["total_shortened"],
                },
            }

            context_reduction_path = output_dir / "_context_reduction.json"
            with open(context_reduction_path, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "solution_context_reduced": solution_context_reduced,
                        "attempt_context_reduced": attempt_context_reduced,
                        "context_reduced_details": context_reduced_details,
                    },
                    f,
                    indent=2,
                )
            logger.info(f"  Context reduction info saved to: {context_reduction_path}")

            effective_ai_attempt_dir = str(ai_shortened_dir)
        else:
            logger.info("  Within limit, no shortening needed.")

        ai_attempt_files = prepare_directory_files(effective_ai_attempt_dir)

    # STEP 3: Build file messages and rubric checks
    logger.info("\n[Step 3] Building conversation via compile_prompt...")

    solution_messages, solution_prompt, solution_file_sizes = format_file_section(
        "Golden solution",
        golden_solution_files,
        add_confirmation=not no_file_check,
    )

    ai_attempt_messages, ai_attempt_prompt, ai_attempt_file_sizes = format_file_section(
        "AI attempt", ai_attempt_files, add_confirmation=not no_file_check
    )

    # Build context messages
    context_messages = []
    context_prompt = ""
    context_file_sizes = {}
    context_display_name = ""
    if context_file_path:
        context_display_name = context_file_path.name
        context_ext = context_file_path.suffix.lower()

        if context_ext == ".txt":
            try:
                with open(context_file_path, "r", encoding="utf-8") as f:
                    context_content = f.read()
                context_text = f"Context:\n{context_content}"
                context_messages = [
                    {
                        "role": "user",
                        "content": [{"type": "text", "text": context_text}],
                    }
                ]
                context_file_sizes[context_file_path.name] = len(context_text)
            except UnicodeDecodeError:
                base64_content, mime_type = encode_file_to_base64(context_file_path)
                context_messages = [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "Context:"},
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:{mime_type};base64,{base64_content}"
                                },
                            },
                        ],
                    }
                ]
                context_file_sizes[context_file_path.name] = len("Context:") + len(
                    base64_content
                )
        else:
            base64_content, mime_type = encode_file_to_base64(context_file_path)
            context_messages = [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Context:"},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:{mime_type};base64,{base64_content}"
                            },
                        },
                    ],
                }
            ]
            context_file_sizes[context_file_path.name] = len("Context:") + len(
                base64_content
            )
        context_prompt = f"Context: {context_file_path.name}\n"
    else:
        context_prompt = "No context file provided.\n"

    if not no_file_check and context_messages:
        context_messages, context_prompt = add_file_confirmation(
            context_messages, header="context_files", prompt=context_prompt
        )

    # Render rubric checks for each category
    accuracy_checks = render_rubric_checks(str(rubric_json_path), "Accuracy")
    formula_checks = render_rubric_checks(str(rubric_json_path), "Formula")
    formatting_checks = render_rubric_checks(str(rubric_json_path), "Formatting")

    # Compile the prompt template into staged message lists
    compile_kwargs = dict(
        ai_attempt="ai_attempt",
        solution_sheet=golden_solution_stem,
        context=context_display_name or None,
        solution_messages=solution_messages,
        ai_attempt_messages=ai_attempt_messages,
        accuracy_checks=accuracy_checks,
        formula_checks=formula_checks,
        formatting_checks=formatting_checks,
    )
    if context_messages:
        compile_kwargs["context_messages"] = context_messages

    stages = compile_prompt(template_path, **compile_kwargs)
    logger.info(f" Compiled {len(stages)} evaluation stages from template")
    # Build check name mapping for enriching responses
    check_name_mapping = build_check_name_mapping(str(rubric_json_path))

    # STEP 4: Save file prompt for logging
    logger.info("\n[Step 4] Saving file prompt...")
    prompt = solution_prompt + ai_attempt_prompt + context_prompt
    fileprompt_path = output_dir / "fileprompt.txt"
    with open(fileprompt_path, "w", encoding="utf-8") as f:
        f.write(prompt)
    logger.info(f" File prompt saved to: {fileprompt_path}")

    if nocall:
        logger.info("\n--nocall flag set. Skipping API calls.")
        remove_log_file(cache_log_path)
        shutil.copy(cache_log_path, str(output_dir / "judge.log"))
        return

    # STEP 5: Make sequential OpenRouter API calls via staged conversation
    logger.info("\n[Step 5] Evaluating with OpenRouter API...")

    all_stage_conversations = {}
    stage_responses = {}  # stage_idx -> response_text
    conversation_messages = []
    token_tracking = {
        "evaluations": {},
        "total_message_size": 0,
        "total_message_size_with_images": 0,
        "total_tokens": 0,
        "total_prompt_tokens": 0,
        "total_completion_tokens": 0,
        "total_cost": 0.0,
        "file_sizes": {
            "golden_solution": solution_file_sizes,
            "ai_attempt": ai_attempt_file_sizes,
            "context": context_file_sizes,
        },
    }

    all_responses = {}
    parse_failures = {}

    for stage_idx, stage_messages in enumerate(stages):
        category = (
            CHECK_ORDER[stage_idx]
            if stage_idx < len(CHECK_ORDER)
            else f"stage_{stage_idx}"
        )

        logger.info(f"  Evaluating {category} (stage {stage_idx})...")

        # Each stage is a self-contained conversation (template defines full context per stage)
        conversation_messages = list(stage_messages)

        # Fill in prior_response slots with actual responses from earlier stages
        for msg in conversation_messages:
            prior_idx = msg.pop("_prior_stage", None)
            if prior_idx is not None:
                msg["content"] = stage_responses[prior_idx]

        # Retry loop for API call + JSON parsing
        max_json_attempts = 10
        parse_success = False
        response_text = None
        failed_responses = []
        cumulative_metrics = {
            "message_size": 0,
            "message_size_with_images": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        }

        for json_attempt in range(max_json_attempts):
            response, metrics = robust_send_message(
                client,
                conversation_messages,
                model,
                response_format={"type": "json_object"},
                reasoning_effort=reasoning_effort,
            )

            response_text = response.choices[0].message.content

            cumulative_metrics["message_size"] += metrics["message_size"]
            cumulative_metrics["message_size_with_images"] += metrics[
                "message_size_with_images"
            ]
            cumulative_metrics["prompt_tokens"] += metrics["prompt_tokens"]
            cumulative_metrics["completion_tokens"] += metrics["completion_tokens"]
            cumulative_metrics["total_tokens"] += metrics["total_tokens"]

            # Try to parse JSON
            try:
                if response_text is None or response_text.strip() == "":
                    raise JudgeOutputError("Response content is empty.")

                json_text = _extract_json_from_response(response_text)
                parsed_response = json.loads(json_text)
                if category in parsed_response:
                    category_data = parsed_response[category]
                else:
                    category_data = parsed_response

                # Format check category_data. It must be a list of check items with 'check', 'decision', 'summary', and 'mistakes' fields.
                # 'check', 'decision', and 'summary' are strings. 'mistakes' is a list of dictionaries. If any field is missing, enforce a retry
                if isinstance(category_data, list):
                    for item in category_data:
                        if not isinstance(item, dict):
                            raise JudgeOutputError(
                                f"Check item is not a dictionary: {item}"
                            )
                        if (
                            "check" not in item
                            or "decision" not in item
                            or "summary" not in item
                            or "mistakes" not in item
                        ):
                            missing_fields = [
                                field
                                for field in [
                                    "check",
                                    "decision",
                                    "summary",
                                    "mistakes",
                                ]
                                if field not in item
                            ]
                            raise JudgeOutputError(
                                f"Check item missing required fields: {missing_fields}. Item: {item}"
                            )
                        if (
                            not isinstance(item["check"], str)
                            or not isinstance(item["decision"], str)
                            or not isinstance(item["summary"], str)
                            or not isinstance(item["mistakes"], list)
                        ):
                            raise JudgeOutputError(
                                f"Check item has incorrect field types: {item}"
                            )
                        for mistake in item["mistakes"]:
                            if not isinstance(mistake, dict):
                                raise JudgeOutputError(
                                    f"Mistake item is not a dictionary: {mistake}"
                                )
                else:
                    raise JudgeOutputError(
                        f"Category data is not a list: {category_data}"
                    )

                # Enrich each check item with its name if available
                if isinstance(category_data, list):
                    for item in category_data:
                        if isinstance(item, dict) and "check" in item:
                            check_letter = item["check"]
                            name = check_name_mapping.get((category, check_letter))
                            if name:
                                item["name"] = name

                all_responses[category] = category_data
                parse_success = True
                break
            except (json.JSONDecodeError, JudgeOutputError) as e:
                traceback.print_exc()
                if response_text is None:
                    response_text = "<empty response>"
                failed_responses.append(response_text)
                parse_failures[category] = {
                    "success": True,
                    "count": len(failed_responses),
                    "responses": failed_responses,
                }
                if json_attempt < max_json_attempts - 1:
                    logger.info(
                        f"   Parse/validation attempt {json_attempt + 1}/{max_json_attempts} "
                        f"failed for {category}: {e}. Response: {response_text[:200]}... retrying..."
                    )
                    time.sleep(2)

        if not parse_success:
            logger.info(
                f"   WARNING: Failed to parse JSON for {category} after "
                f"{max_json_attempts} attempts. Raw response: {response_text[:200]}..."
            )
            parse_failures[category]["success"] = False
            all_responses[category] = {"raw_response": response_text}

        # Track cumulative metrics
        token_tracking["evaluations"][category] = cumulative_metrics
        token_tracking["evaluations"][category]["chars_per_token"] = (
            round(
                cumulative_metrics["message_size"]
                / cumulative_metrics["prompt_tokens"],
                2,
            )
            if cumulative_metrics["prompt_tokens"] > 0
            else 0
        )
        cost_info = calculate_cost(
            model,
            cumulative_metrics["prompt_tokens"],
            cumulative_metrics["completion_tokens"],
        )
        token_tracking["evaluations"][category]["cost"] = cost_info["total_cost"]
        token_tracking["total_message_size"] += cumulative_metrics["message_size"]
        token_tracking["total_message_size_with_images"] += cumulative_metrics[
            "message_size_with_images"
        ]
        token_tracking["total_tokens"] += cumulative_metrics["total_tokens"]
        token_tracking["total_prompt_tokens"] += cumulative_metrics["prompt_tokens"]
        token_tracking["total_completion_tokens"] += cumulative_metrics[
            "completion_tokens"
        ]
        token_tracking["total_cost"] += cost_info["total_cost"]

        logger.info(
            f"   Message size: {cumulative_metrics['message_size']:,} chars "
            f"(with images: {cumulative_metrics['message_size_with_images']:,}) | "
            f"Tokens: {cumulative_metrics['prompt_tokens']:,} prompt + "
            f"{cumulative_metrics['completion_tokens']} completion = "
            f"{cumulative_metrics['total_tokens']:,} total | "
            f"Cost: ${cost_info['total_cost']:.6f}"
        )

        conversation_messages.append({"role": "assistant", "content": response_text})
        stage_responses[stage_idx] = response_text
        all_stage_conversations[category] = conversation_messages

        logger.info(f"   {category} evaluation completed")
        time.sleep(0.5)

    # Save conversation messages for reference (one file per stage)
    conversation_logs_dir = output_dir / "judge_conversation_logs"
    conversation_logs_dir.mkdir(parents=True, exist_ok=True)
    for stage_category, stage_msgs in all_stage_conversations.items():
        conversation_path = (
            conversation_logs_dir / f"conversation_messages_{stage_category}.json"
        )
        with open(conversation_path, "w", encoding="utf-8") as f:
            json.dump(stage_msgs, f, indent=2)
        yaml_path = conversation_path.with_suffix(".yaml")
        dump_messages_yaml(stage_msgs, yaml_path)
        logger.info(f" Conversation messages saved to: {conversation_path}")
        logger.info(f" Conversation messages saved to: {yaml_path}")

    # Shared finalization: save judgement, scores, metadata, logs
    return _finalize_case(
        all_responses=all_responses,
        output_dir=output_dir,
        weights_data=weights_data,
        token_tracking=token_tracking,
        model=model,
        attempt_model=attempt_model,
        task_folder_name=task_folder_name,
        golden_solution_files=golden_solution_files,
        ai_attempt_files=ai_attempt_files,
        context_file_path=context_file_path,
        start_time=start_time,
        cache_log_path=cache_log_path,
        versions=versions,
        golden_solution_dir=golden_solution_dir,
        ai_attempt_dir=ai_attempt_dir,
        parse_failures=parse_failures,
        solution_context_reduced=solution_context_reduced,
        attempt_context_reduced=attempt_context_reduced,
        context_reduced_details=context_reduced_details,
        reasoning_effort=reasoning_effort,
    )


### Shared Case Helpers


def _delete_ignored_sheet_files(directories, ignore_sheets):
    """Delete CSV/format files for sheets in *ignore_sheets* from each directory.

    Files are named ``<safe_sheet_name>_full.csv`` and
    ``<safe_sheet_name>_additional_format.txt``; matching is case-insensitive
    against the file stem (i.e. the safe sheet name). Applies uniformly to
    fresh extractions and to dirs populated from the persistent CSV cache.
    """
    if not ignore_sheets:
        return
    targets = {s.lower() for s in ignore_sheets}
    for d in directories:
        if not d:
            continue
        d_path = Path(d)
        if not d_path.exists():
            continue
        for csv_path in d_path.glob("*_full.csv"):
            stem = csv_path.name.removesuffix("_full.csv")
            if stem.lower() in targets:
                csv_path.unlink()
                fmt = d_path / f"{stem}_additional_format.txt"
                fmt.unlink(missing_ok=True)
                logger.info(f"  Ignored sheet '{stem}': removed from {d_path}")


def _prepare_case(
    task_folder: str,
    rubric_path: str,
    rubric_weight_path: str = None,
    use_existing: bool = True,
    run_calculation: bool = False,
    cached_solution_csv_dir: str = None,
    cached_attempt_csv_dir: str = None,
    attempt_sheet_name_filter: bool = False,
    ignore_sheets: list[str] | None = None,
    agentic: bool = False,
) -> dict:
    """Shared setup for judge workflows: logging, validation, file processing.

    Handles:
    - Cache directory and log file setup
    - Version info loading (agentic mode reads from AGENTIC_JUDGE_* env vars)
    - Rubric/weights validation (Step 0)
    - Case file processing: xlsx to CSV extraction (Step 1)
    - Support file copying

    Returns a dict with all state needed by downstream judge functions.
    """
    cache_dir = (
        Path(load_env_var("PATHS_SCRATCH_PATH", default="scratch"))
        / "judge_cache"
        / f"run_{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"
    )
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_log_path = str(cache_dir / "judge.log")
    add_log_file(cache_log_path)
    logger.info(f"Writing logs to temporary cache path: {cache_log_path}")

    task_folder_name = Path(task_folder).name
    task_path = Path(task_folder)

    if agentic:
        JUDGE_VERSION = load_env_var("AGENTIC_JUDGE_VERSION", required=True)
        PROMPT_VERSION = load_env_var("AGENTIC_JUDGE_PROMPT_VERSION", required=True)
    else:
        JUDGE_VERSION = load_env_var("JUDGE_VERSION", required=True)
        PROMPT_VERSION = load_env_var("JUDGE_PROMPT_VERSION", required=True)
    RUBRIC_VERSION = load_env_var("JUDGE_RUBRIC_VERSION", required=True)
    RUBRIC_WEIGHT_VERSION = load_env_var("JUDGE_RUBRIC_WEIGHT_VERSION", default=None)
    CHECK_ORDER = load_env_var(
        "JUDGE_CHECK_ORDER", default="Accuracy,Formula,Formatting"
    ).split(",")

    start_time = time.time()

    # Step 0: Validate rubric/weights
    weights_data = None
    if rubric_weight_path:
        logger.info("\n[Step 0] Validating rubric/weights consistency...")
        try:
            validate_rubric_weights_consistency(rubric_path, rubric_weight_path)
            logger.info("  Rubric and weights files are consistent.")
            with open(rubric_weight_path, "r", encoding="utf-8") as f:
                weights_data = json.load(f)
        except RubricWeightConsistencyError as e:
            logger.info(f"  ERROR: {e}")
            raise

    # Step 1: Process case files
    logger.info("\n[Step 1] Processing case files...")
    try:
        golden_solution_path = find_golden_solution_file(task_path)
        logger.info(f"  Golden solution file: {golden_solution_path.name}")
    except Exception as e:
        logger.info(f"  Error finding golden solution file: {e}")
        raise

    ai_attempt_path = task_path / "ai_attempt.xlsx"
    golden_solution_stem = golden_solution_path.stem

    files_to_process = []
    if not cached_attempt_csv_dir:
        files_to_process.append(str(ai_attempt_path))
    if not cached_solution_csv_dir:
        files_to_process.append(str(golden_solution_path))

    if cached_solution_csv_dir:
        logger.info(f"  Using cached solution CSVs from: {cached_solution_csv_dir}")
    if cached_attempt_csv_dir:
        logger.info(f"  Using cached attempt CSVs from: {cached_attempt_csv_dir}")

    # When a cache is used, the extraction for that file is skipped but
    # `use_existing` must still be True for the remaining file so prior
    # extractions are honored.
    effective_use_existing = (
        True if (cached_solution_csv_dir or cached_attempt_csv_dir) else use_existing
    )
    result = process_case_files(
        files_to_process,
        task_folder,
        use_existing=effective_use_existing,
        run_calculation=run_calculation,
        attempt_sheet_name_filter=attempt_sheet_name_filter,
    )
    output_dir = result["output_dir"]
    workbook_dirs = result.get("workbook_dirs", {})

    if cached_solution_csv_dir:
        dest_solution_dir = output_dir / golden_solution_stem
        if not dest_solution_dir.exists():
            shutil.copytree(cached_solution_csv_dir, str(dest_solution_dir))
        workbook_dirs[golden_solution_stem] = str(dest_solution_dir)

        cached_files = sorted(
            f.name for f in Path(dest_solution_dir).iterdir() if f.is_file()
        )
        logger.info(
            f"  Copied {len(cached_files)} cached solution CSV files to: "
            f"{dest_solution_dir}"
        )
        for fname in cached_files:
            logger.info(f"    {fname}")

    if cached_attempt_csv_dir:
        dest_attempt_dir = output_dir / "ai_attempt"
        if not dest_attempt_dir.exists():
            shutil.copytree(cached_attempt_csv_dir, str(dest_attempt_dir))
        workbook_dirs["ai_attempt"] = str(dest_attempt_dir)

        cached_files = sorted(
            f.name for f in Path(dest_attempt_dir).iterdir() if f.is_file()
        )
        logger.info(
            f"  Copied {len(cached_files)} cached attempt CSV files to: "
            f"{dest_attempt_dir}"
        )
        for fname in cached_files:
            logger.info(f"    {fname}")

    logger.info(f"  Files processed and saved to: {output_dir}")

    copied_files = copy_support_files(
        task_path,
        output_dir,
        default_rubric_path=rubric_path,
    )
    logger.info(f"  Copied {len(copied_files)} support files to output directory")

    ai_attempt_dir = workbook_dirs.get("ai_attempt")
    golden_solution_dir = workbook_dirs.get(golden_solution_stem)

    # Drop sheets the caller asked to ignore (e.g. cover sheets). Applied
    # after both fresh extraction and cache copy so the artifact on disk
    # reflects exactly what the judge will see.
    _delete_ignored_sheet_files(
        [ai_attempt_dir, golden_solution_dir], ignore_sheets
    )

    # Detect context file
    context_file_path = None
    context_pdf = output_dir / "context.pdf"
    context_txt = output_dir / "context.txt"
    if context_pdf.exists():
        context_file_path = context_pdf
    elif context_txt.exists():
        context_file_path = context_txt

    rubric_json_path = output_dir / "rubric.json"

    return {
        "cache_dir": cache_dir,
        "cache_log_path": cache_log_path,
        "task_folder_name": task_folder_name,
        "task_path": task_path,
        "output_dir": output_dir,
        "workbook_dirs": workbook_dirs,
        "golden_solution_path": golden_solution_path,
        "golden_solution_stem": golden_solution_stem,
        "ai_attempt_dir": ai_attempt_dir,
        "golden_solution_dir": golden_solution_dir,
        "weights_data": weights_data,
        "context_file_path": context_file_path,
        "rubric_json_path": rubric_json_path,
        "start_time": start_time,
        "versions": {
            "JUDGE_VERSION": JUDGE_VERSION,
            "PROMPT_VERSION": PROMPT_VERSION,
            "RUBRIC_VERSION": RUBRIC_VERSION,
            "RUBRIC_WEIGHT_VERSION": RUBRIC_WEIGHT_VERSION,
        },
        "CHECK_ORDER": CHECK_ORDER,
    }


def _finalize_case(
    all_responses,
    output_dir,
    weights_data,
    token_tracking,
    model,
    attempt_model,
    task_folder_name,
    golden_solution_files,
    ai_attempt_files,
    context_file_path,
    start_time,
    cache_log_path,
    versions,
    golden_solution_dir=None,
    ai_attempt_dir=None,
    parse_failures=None,
    solution_context_reduced=False,
    attempt_context_reduced=False,
    context_reduced_details=None,
    agentic=False,
    auto_routed=False,
    reasoning_effort=None,
):
    """Shared finalization: save judgement, calculate scores, write metadata."""
    output_dir = Path(output_dir)

    # Warn if expected token_tracking keys are missing
    _expected_tt_keys = {
        "total_message_size",
        "total_message_size_with_images",
        "total_tokens",
        "total_prompt_tokens",
        "total_completion_tokens",
        "total_cost",
        "evaluations",
    }
    _missing_tt = _expected_tt_keys - set(token_tracking.keys())
    if _missing_tt:
        logger.warning(
            f"  token_tracking missing expected keys: {sorted(_missing_tt)}. "
            f"Defaulting to 0 for missing values."
        )

    if golden_solution_files is None:
        logger.warning("  golden_solution_files is None; expected a dict.")
        golden_solution_files = {}
    if ai_attempt_files is None:
        logger.warning("  ai_attempt_files is None; expected a dict.")
        ai_attempt_files = {}

    # Save ai_judgement
    logger.info("\n[Save] Saving AI judgement...")
    ai_judgement_path = output_dir / "ai_judgement.json"
    with open(ai_judgement_path, "w", encoding="utf-8") as f:
        json.dump(all_responses, f, indent=2)
    logger.info(f"  AI judgement saved to: {ai_judgement_path}")

    # Calculate scores
    score_results = None
    if weights_data:
        logger.info("\n[Score] Calculating scores...")
        score_results = calculate_scores(
            all_responses, weights_data, max_mistakes=RUBRIC_MAX_MISTAKES
        )
        scores_path = output_dir / "scores.json"
        with open(scores_path, "w", encoding="utf-8") as f:
            json.dump(score_results, f, indent=2)
        logger.info(f"  Scores saved to: {scores_path}")

        logger.info("\n  Score Summary:")
        for cat in ["Accuracy", "Formula", "Formatting"]:
            if cat in score_results["criteria_scores"]:
                cs = score_results["criteria_scores"][cat]
                logger.info(
                    f"    {cat}: {cs['normalized_score']:.2f}/100 "
                    f"(weight: {cs['category_weight']:.2f}, "
                    f"contribution: {cs['normalized_score'] * cs['category_weight']:.2f})"
                )
        logger.info(f"    TOTAL: {score_results['total_score']:.2f}/100")

    # Save token tracking
    token_tracking["model"] = model
    token_tracking_path = output_dir / "token_tracking.json"
    with open(token_tracking_path, "w", encoding="utf-8") as f:
        json.dump(token_tracking, f, indent=2)
    logger.info(f"  Token tracking saved to: {token_tracking_path}")

    # Create metadata
    elapsed_time = time.time() - start_time
    metadata_path = output_dir / "_metadata.json"
    metadata_dict = {
        "task_folder": task_folder_name,
        "grader_model": model,
        "judge_reasoning": reasoning_effort,
        "judge_mode": "agentic" if agentic else "non-agentic",
        "auto_routed": auto_routed,
        "attempt_model": attempt_model,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "judge_version": versions["JUDGE_VERSION"],
        "prompt_version": versions["PROMPT_VERSION"],
        "rubric_version": versions["RUBRIC_VERSION"],
        "rubric_weight_version": versions["RUBRIC_WEIGHT_VERSION"],
        "rubric_max_mistakes": RUBRIC_MAX_MISTAKES,
        "total_prompt_tokens": token_tracking.get("total_prompt_tokens", 0),
        "total_completion_tokens": token_tracking.get("total_completion_tokens", 0),
        "total_tokens": token_tracking.get("total_tokens", 0),
        "total_cost": round(token_tracking.get("total_cost", 0), 6),
        "elapsed_time_seconds": round(elapsed_time, 2),
        "files_considered": {
            "golden_solution": (
                sorted(golden_solution_files.keys()) if golden_solution_files else []
            ),
            "ai_attempt": (sorted(ai_attempt_files.keys()) if ai_attempt_files else []),
            "context": context_file_path.name if context_file_path else None,
        },
    }
    if score_results:
        metadata_dict["total_score"] = score_results["total_score"]
        metadata_dict["criteria_scores"] = {
            cat: data["normalized_score"]
            for cat, data in score_results["criteria_scores"].items()
        }

    if metadata_path.exists():
        try:
            with open(metadata_path, "r", encoding="utf-8") as f:
                existing_metadata = json.load(f)
            if isinstance(existing_metadata, dict):
                existing_metadata.update(metadata_dict)
                metadata_dict = existing_metadata
        except (json.JSONDecodeError, IOError):
            pass

    with open(metadata_path, "w", encoding="utf-8") as f:
        json.dump(metadata_dict, f, indent=2)
    logger.info(f"  Metadata saved to: {metadata_path}")

    # Log summary
    logger.info("\n" + "=" * 80)
    logger.info("EVALUATION COMPLETE")
    logger.info("=" * 80)
    total_msg_size = token_tracking.get("total_message_size", 0)
    total_msg_size_img = token_tracking.get("total_message_size_with_images", 0)
    total_tokens = token_tracking.get("total_tokens", 0)
    logger.info(f"\nToken Usage & Cost Summary:")
    logger.info(
        f"  Total message size: {total_msg_size:,} characters "
        f"(with images: {total_msg_size_img:,})"
    )
    logger.info(f"  Total tokens used: {total_tokens:,}")
    logger.info(
        f"    - Prompt tokens: {token_tracking.get('total_prompt_tokens', 0):,}"
    )
    logger.info(
        f"    - Completion tokens: "
        f"{token_tracking.get('total_completion_tokens', 0):,}"
    )
    if total_tokens > 0:
        logger.info(f"  Average ratio: {total_msg_size / total_tokens:.2f} chars/token")
    logger.info(f"  Total cost: ${token_tracking.get('total_cost', 0):.6f}")
    if token_tracking.get("evaluations"):
        logger.info("\n  Evaluations:")
        for cat, data in token_tracking["evaluations"].items():
            logger.info(
                f"    {cat}: {data['message_size']:,} chars "
                f"(with images: {data.get('message_size_with_images', 0):,}) -> "
                f"{data['total_tokens']:,} tokens "
                f"({data.get('chars_per_token', 0):.2f} chars/token) | "
                f"${data.get('cost', 0):.6f}"
            )
    logger.info("=" * 80)

    # Build result
    result = {
        "ai_judgement": str(ai_judgement_path),
        "output_dir": str(output_dir),
        "solution_csv_dir": golden_solution_dir,
        "attempt_csv_dir": ai_attempt_dir,
        "solution_context_reduced": solution_context_reduced,
        "attempt_context_reduced": attempt_context_reduced,
        "context_reduced_details": context_reduced_details,
        "auto_routed": auto_routed,
    }
    if score_results:
        result["score_results"] = score_results
        result["accuracy_score"] = (
            score_results["criteria_scores"].get("Accuracy", {}).get("normalized_score")
        )
        result["formula_score"] = (
            score_results["criteria_scores"].get("Formula", {}).get("normalized_score")
        )
        result["formatting_score"] = (
            score_results["criteria_scores"]
            .get("Formatting", {})
            .get("normalized_score")
        )
        result["final_score"] = score_results["total_score"]

        scoring_warnings = score_results.get("scoring_warnings") or {}
        if any(scoring_warnings.get(k) for k in scoring_warnings):
            result["scoring_warnings"] = scoring_warnings
            logger.info("\nScoring Warnings Summary:")
            if scoring_warnings.get("unscored_checks"):
                logger.info("  Unscored checks (weighted but not evaluated):")
                for cat, names in scoring_warnings["unscored_checks"].items():
                    logger.info(f"    {cat}: {names}")
            if scoring_warnings.get("empty_category_judgements"):
                logger.info(
                    f"  Empty category judgements: "
                    f"{scoring_warnings['empty_category_judgements']}"
                )
            if scoring_warnings.get("duplicate_judgements"):
                logger.info("  Duplicate judgement names (kept max mistakes):")
                for cat, dupes in scoring_warnings["duplicate_judgements"].items():
                    for name, ms in dupes.items():
                        logger.info(f"    {cat}/{name}: reported mistakes={ms}")
            if scoring_warnings.get("mistake_count_mismatches"):
                logger.info(
                    "  total_mistakes vs len(mistakes) mismatches "
                    "(using len(mistakes)):"
                )
                for m in scoring_warnings["mistake_count_mismatches"]:
                    logger.info(
                        f"    {m['category']}/{m['name']}: "
                        f"claimed={m['claimed_total_mistakes']} "
                        f"actual={m['actual_mistakes_len']}"
                    )
            if scoring_warnings.get("fail_without_mistakes"):
                logger.info(
                    "  Decision='fail' with no mistakes recorded "
                    "(scored as max_mistakes):"
                )
                for f in scoring_warnings["fail_without_mistakes"]:
                    logger.info(f"    {f['category']}/{f['name']}")

    if parse_failures:
        result["parse_failures"] = parse_failures
        logger.info("\nJSON Parse Failures Summary:")
        for category, info in parse_failures.items():
            logger.info(
                f"  {category}: {info['count']} failed parse attempts recorded."
            )
            if info["count"] >= 1:
                logger.info(
                    f"    Sample failed response: {info['responses'][0][:500]}..."
                )

    remove_log_file(cache_log_path)
    shutil.copy(cache_log_path, str(output_dir / "judge.log"))
    return result


### Agentic Judge

AGENTIC_JUDGE_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": (
                "Read a rectangular range from a CSV file in the AI attempt or "
                "golden solution directories. You must specify the row and column "
                "range to extract. Use the file metadata provided in the prompt "
                "(dimensions and additional_format.txt) to decide which ranges to "
                "inspect. HARD LIMIT: a single call may not cover more than "
                "5000 cells ((end_row - start_row + 1) * (end_col - start_col + "
                "1)). Requests exceeding this are rejected; split large regions "
                "into multiple smaller calls."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "source": {
                        "type": "string",
                        "enum": ["attempt", "solution"],
                        "description": (
                            "Which directory to read from: 'attempt' for the AI "
                            "attempt workbook, 'solution' for the golden solution."
                        ),
                    },
                    "filename": {
                        "type": "string",
                        "description": (
                            "The CSV filename to read, e.g. 'Sheet1_full.csv'."
                        ),
                    },
                    "start_row": {
                        "type": "integer",
                        "description": (
                            "First row to include (1-based, inclusive). Row 1 is "
                            "the first row of the spreadsheet."
                        ),
                    },
                    "end_row": {
                        "type": "integer",
                        "description": ("Last row to include (1-based, inclusive)."),
                    },
                    "start_col": {
                        "type": "string",
                        "description": (
                            "First column letter to include (inclusive), e.g. 'A'."
                        ),
                    },
                    "end_col": {
                        "type": "string",
                        "description": (
                            "Last column letter to include (inclusive), e.g. 'Z'."
                        ),
                    },
                },
                "required": [
                    "source",
                    "filename",
                    "start_row",
                    "end_row",
                    "start_col",
                    "end_col",
                ],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "record_check",
            "description": (
                "Record (upsert) a pass/fail decision and summary for a rubric "
                "check in the current category. Must be called for every check "
                "letter before you stop. Calling again for the same letter "
                "overwrites the decision/summary but preserves any mistakes "
                "already appended via append_mistake."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "check": {
                        "type": "string",
                        "description": ("The rubric check letter (e.g. 'A')."),
                    },
                    "decision": {
                        "type": "string",
                        "enum": ["pass", "fail"],
                        "description": "Your pass/fail decision for this check.",
                    },
                    "summary": {
                        "type": "string",
                        "description": (
                            "Brief explanation of your assessment for this check."
                        ),
                    },
                },
                "required": ["check", "decision", "summary"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "append_mistake",
            "description": (
                "Append a single mistake to a check that has already been "
                "recorded via record_check. Call once per mistake. The "
                "corresponding record_check must have been called first."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "check": {
                        "type": "string",
                        "description": "The rubric check letter (e.g. 'A').",
                    },
                    "location": {
                        "type": "string",
                        "description": (
                            "Cell / range reference where the mistake occurs "
                            "(e.g. 'Sheet1!B7')."
                        ),
                    },
                    "description": {
                        "type": "string",
                        "description": "What is wrong.",
                    },
                    "severity": {
                        "type": "string",
                        "enum": ["minor", "major"],
                        "description": "Severity of the mistake.",
                    },
                },
                "required": ["check", "location", "description", "severity"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_working_judgement",
            "description": (
                "Return the current working judgement state for this category "
                "as JSON. Useful to review what you've recorded so far before "
                "finalizing."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "evict_tool_results",
            "description": (
                "Drop all prior rounds' read_file / scratchpad tool-call and "
                "tool-result messages from the wire context (rounds strictly "
                "before before_round). The findings from those reads should "
                "already have been absorbed into your working judgement via "
                "record_check / append_mistake. Use this when the context "
                "pressure signal tells you context is filling up."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "before_round": {
                        "type": "integer",
                        "description": (
                            "Evict every tool-call/tool-result pair whose round "
                            "index is strictly less than this number. Round "
                            "numbering starts at 1 for the first API call."
                        ),
                    },
                },
                "required": ["before_round"],
            },
        },
    },
]


def _build_file_metadata(directory: str) -> dict:
    """Build metadata dict for CSV files in a directory.

    Returns:
        dict mapping filenames to {"format_info": str|None}.
        Only includes *_full.csv files. format_info is the content of the
        corresponding *_additional_format.txt file (which already contains
        sheet dimensions, merged cells, and frozen panes info).
    """
    metadata = {}
    if not directory or not Path(directory).exists():
        return metadata

    dir_path = Path(directory)
    for csv_file in sorted(dir_path.glob("*_full.csv")):
        base_name = csv_file.stem.replace("_full", "")
        format_txt = dir_path / f"{base_name}_additional_format.txt"
        format_info = None
        if format_txt.exists():
            try:
                format_info = format_txt.read_text(encoding="utf-8")
            except Exception:
                pass
        metadata[csv_file.name] = {
            "format_info": format_info,
        }
    return metadata


def _col_letter_to_index(col_str: str) -> int:
    """Convert an Excel column letter (e.g. 'A', 'Z', 'AA') to a 1-based index."""
    col_str = col_str.upper().strip()
    result = 0
    for ch in col_str:
        result = result * 26 + (ord(ch) - ord("A") + 1)
    return result


def _render_files_text(file_list, file_metadata=None) -> str:
    """Render sorted file list with optional format_info metadata as indented text."""
    lines = []
    for fname in file_list:
        lines.append(f"    {fname}")
        meta = (file_metadata or {}).get(fname)
        if meta and meta.get("format_info") and fname.endswith("_full.csv"):
            for line in meta["format_info"].splitlines():
                lines.append(f"      {line}")
    return "\n".join(lines)


def _render_prior_findings_block(prior_findings) -> str | None:
    if not prior_findings:
        return None
    return f"Findings from prior categories (for reference):\n{prior_findings}\n\n"


def _build_agentic_context_messages(context_file_path) -> list[dict]:
    """Build context messages for the agentic judge, supporting .txt and PDFs.

    PDFs (and other binary types) are base64-encoded and attached via
    image_url, mirroring the standard judge's context handling. Returns an
    empty list if no context file is provided.
    """
    if not context_file_path:
        return []

    context_file_path = Path(context_file_path)
    ext = context_file_path.suffix.lower()

    if ext == ".txt":
        try:
            with open(context_file_path, "r", encoding="utf-8") as f:
                content = f.read()
            return [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": f"Case context:\n{content}"},
                    ],
                }
            ]
        except UnicodeDecodeError:
            logger.warning(f"  Could not read context file: {context_file_path}")
            # Fall through to base64 encoding below
            pass

    base64_content, mime_type = encode_file_to_base64(context_file_path)
    return [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": f"Case context ({context_file_path.name}):"},
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{mime_type};base64,{base64_content}"},
                },
            ],
        }
    ]


def _measure_message_chars(msg) -> int:
    """Return the character count of a single conversation message.

    Handles plain dicts (system/user/tool messages) and SDK response objects
    (assistant messages that may carry tool_calls with arguments).
    """
    total = 0

    # Dict messages (system, user, tool, or manually constructed assistant)
    if isinstance(msg, dict):
        content = msg.get("content")
        if isinstance(content, str):
            total += len(content)
        elif isinstance(content, list):
            # Multi-part content (e.g. text + image blocks)
            for part in content:
                if isinstance(part, dict):
                    total += len(part.get("text", ""))
        # Manually-constructed tool_calls (shouldn't normally happen, but be safe)
        for tc in msg.get("tool_calls", []):
            if isinstance(tc, dict):
                func = tc.get("function", {})
                total += len(func.get("name", ""))
                total += len(func.get("arguments", ""))
        return total

    # SDK ChatCompletionMessage objects (from choice.message)
    content = getattr(msg, "content", None)
    if isinstance(content, str):
        total += len(content)

    tool_calls = getattr(msg, "tool_calls", None)
    if tool_calls:
        for tc in tool_calls:
            func = getattr(tc, "function", None)
            if func:
                total += len(getattr(func, "name", "") or "")
                total += len(getattr(func, "arguments", "") or "")

    return total


def _msg_to_dict(m):
    """Convert an SDK message or plain dict to a plain dict for logging."""
    if isinstance(m, dict):
        return m

    # Handles ChatCompletionMessage (pydantic v2) — preserves role, content,
    # tool_calls, and any reasoning fields rather than stringifying.
    if hasattr(m, "model_dump"):
        return m.model_dump(exclude_none=True)

    logger.warning(
        f"  Warning: encountered non-dict message of type {type(m)}; \n content: {m}"
    )

    return {"role": "unknown", "content": str(m)}


class WorkingJudgement:
    """Per-category scratchpad state mutated by record_check / append_mistake."""

    def __init__(self, category: str, check_letters: list[str]):
        self.category = category
        self.check_letters = list(check_letters)
        self.working: dict[str, dict] = {}
        self.pending: set[str] = set(self.check_letters)

    def record_check(self, letter: str, decision: str, summary: str) -> str:
        if letter not in self.check_letters:
            return (
                f"Error: unknown check '{letter}' for {self.category}. "
                f"Valid letters: {', '.join(self.check_letters)}."
            )
        if decision not in ("pass", "fail"):
            return f"Error: decision must be 'pass' or 'fail' (got '{decision}')."
        if letter in self.working:
            self.working[letter]["decision"] = decision
            self.working[letter]["summary"] = summary
        else:
            self.working[letter] = {
                "check": letter,
                "decision": decision,
                "summary": summary,
                "mistakes": [],
            }
        self.pending.discard(letter)
        return (
            f"Recorded check {letter} as {decision}. Coverage: {self.coverage_str()}."
        )

    def append_mistake(
        self, letter: str, location: str, description: str, severity: str
    ) -> str:
        if letter not in self.check_letters:
            return (
                f"Error: unknown check '{letter}' for {self.category}. "
                f"Valid letters: {', '.join(self.check_letters)}."
            )
        if letter not in self.working:
            return (
                f"Error: cannot append_mistake to '{letter}' before "
                f"record_check. Call record_check('{letter}', ...) first."
            )
        if severity not in ("minor", "major"):
            return f"Error: severity must be 'minor' or 'major' (got '{severity}')."
        self.working[letter]["mistakes"].append(
            {
                "location": location,
                "description": description,
                "severity": severity,
            }
        )
        count = len(self.working[letter]["mistakes"])
        return (
            f"Appended mistake to {letter} (now {count} mistake"
            f"{'s' if count != 1 else ''}). "
            f"Coverage: {self.coverage_str()}."
        )

    def coverage_str(self) -> str:
        marks = " ".join(f"{l}\u2713" for l in self.check_letters if l in self.working)
        pending_letters = [l for l in self.check_letters if l in self.pending]
        if pending_letters:
            return f"{marks or '(none yet)'} | pending: {', '.join(pending_letters)}"
        return marks or "(none)"

    def finalize(self) -> list[dict]:
        return [self.working[l] for l in self.check_letters if l in self.working]


def _execute_scratchpad_tool(tool_call, working: WorkingJudgement) -> str:
    """Dispatch record_check / append_mistake / get_working_judgement calls."""
    name = tool_call.function.name
    try:
        args = json.loads(tool_call.function.arguments or "{}")
    except json.JSONDecodeError as e:
        return f"Error: could not parse tool arguments JSON: {e}"

    if name == "record_check":
        letter = str(args.get("check", "")).strip().upper()
        decision = str(args.get("decision", "")).strip().lower()
        summary = str(args.get("summary", ""))
        return working.record_check(letter, decision, summary)

    if name == "append_mistake":
        letter = str(args.get("check", "")).strip().upper()
        location = str(args.get("location", ""))
        description = str(args.get("description", ""))
        severity = str(args.get("severity", "")).strip().lower()
        return working.append_mistake(letter, location, description, severity)

    if name == "get_working_judgement":
        return json.dumps(
            {
                "category": working.category,
                "working": {
                    l: working.working[l]
                    for l in working.check_letters
                    if l in working.working
                },
                "pending": [l for l in working.check_letters if l in working.pending],
            },
            indent=2,
        )

    return f"Error: unknown scratchpad tool '{name}'."


class AgenticCategoryLoop:
    """Holds wire messages, append-only transcript, and per-round bookkeeping.

    Wire (`messages`) is what gets sent to the API and can be mutated by
    eviction. `transcript` is append-only and never reordered; evicted
    entries are flagged with `_evicted_at_round` rather than deleted.
    Every mutation to `messages` goes through `append_wire` so the two stay
    in sync.
    """

    def __init__(self, category: str, check_letters: list[str], seed_messages: list):
        self.category = category
        self.working = WorkingJudgement(category, check_letters)
        self.messages: list = list(seed_messages)
        # parallel meta to `messages`: {"round": int, "tag": str|None}
        self.message_meta: list[dict] = [
            {"round": 0, "tag": "setup"} for _ in seed_messages
        ]
        self.transcript: list[dict] = [
            {
                "type": "msg",
                "msg": _msg_to_dict(m),
                "tag": "setup",
                "round": 0,
                "_evicted_at_round": None,
            }
            for m in seed_messages
        ]
        self.round: int = 0
        self.last_prompt_tokens: int = 0

    def append_wire(self, msg, tag: str | None = None) -> None:
        self.messages.append(msg)
        self.message_meta.append({"round": self.round, "tag": tag})
        self.transcript.append(
            {
                "type": "msg",
                "msg": _msg_to_dict(msg),
                "tag": tag,
                "round": self.round,
                "_evicted_at_round": None,
            }
        )

    def log_event(self, event_type: str, **data) -> None:
        self.transcript.append(
            {
                "type": "event",
                "event": event_type,
                "round": self.round,
                **data,
            }
        )

    def append_synthetic_final(self, judgement: list[dict]) -> None:
        """Record the reconstructed final judgement in the transcript."""
        payload = json.dumps({self.category: judgement}, indent=2)
        self.transcript.append(
            {
                "type": "msg",
                "msg": {"role": "assistant", "content": payload},
                "tag": "synthetic_final",
                "round": self.round,
                "_evicted_at_round": None,
            }
        )

    def evict(self, before_round: int) -> str:
        """Drop tool-call / tool-result pairs with round < before_round."""
        to_evict_indices = [
            i
            for i, meta in enumerate(self.message_meta)
            if 0 < meta["round"] < before_round
            and meta.get("tag") in ("model_tool_call", "tool_result")
        ]
        if not to_evict_indices:
            return (
                "No eligible rounds to evict. evict_tool_results only drops "
                "tool-call/tool-result pairs from rounds before the given "
                "round that still remain in wire context."
            )

        dropped_chars = sum(
            _measure_message_chars(self.messages[i]) for i in to_evict_indices
        )
        rounds_evicted = sorted(
            {self.message_meta[i]["round"] for i in to_evict_indices}
        )
        first_r, last_r = rounds_evicted[0], rounds_evicted[-1]
        stub = {
            "role": "user",
            "content": (
                f"[evicted rounds {first_r}-{last_r} - read_file results "
                f"absorbed into working judgement]"
            ),
        }
        evict_set = set(to_evict_indices)
        new_msgs, new_meta = [], []
        inserted = False
        for i, (m, meta) in enumerate(zip(self.messages, self.message_meta)):
            if i in evict_set:
                if not inserted:
                    new_msgs.append(stub)
                    new_meta.append({"round": self.round, "tag": "evict_stub"})
                    inserted = True
                continue
            new_msgs.append(m)
            new_meta.append(meta)
        self.messages = new_msgs
        self.message_meta = new_meta

        for entry in self.transcript:
            if entry.get("type") != "msg":
                continue
            if entry.get("_evicted_at_round") is not None:
                continue
            if entry.get("tag") not in ("model_tool_call", "tool_result"):
                continue
            if first_r <= entry.get("round", -1) <= last_r:
                entry["_evicted_at_round"] = self.round

        self.log_event(
            "evict",
            dropped_rounds=[first_r, last_r],
            dropped_messages=len(to_evict_indices),
            dropped_chars=dropped_chars,
        )
        return (
            f"Evicted {len(rounds_evicted)} round(s) ({first_r}-{last_r}). "
            f"Wire context reduced by ~{dropped_chars // 1000}K chars."
        )


def _wire_char_total(messages) -> int:
    """Sum of character counts across all wire messages."""
    return sum(_measure_message_chars(m) for m in messages)


def _estimate_wire_tokens(wire_chars: int, chars_per_token: float) -> int:
    """Convert wire chars to an estimated token count using a calibrated ratio.

    `chars_per_token` is computed from the most recent API call's
    ``usage.prompt_tokens`` vs. the wire chars at call time. Falls back to
    a conservative 2.5 when no calibration is available (CSV-dense content
    is ~1-2 chars/token; English prose is ~4).
    """
    cpt = chars_per_token if chars_per_token and chars_per_token > 0 else 2.5
    return int(wire_chars / cpt)


def _build_pressure_signal(
    prompt_tokens: int, limit: int, rounds_elapsed: int
) -> tuple[str, str]:
    """Return (status_line, tier) for the current context pressure level.

    Tier is one of 'low' (<10%), 'advisory' (10-20%), 'strong' (20-80%),
    'forced' (>=80%).
    """
    pct = (prompt_tokens / limit * 100) if limit > 0 else 0.0

    def _k(n: int) -> str:
        return f"~{n // 1000}K" if n >= 1000 else f"~{n}"

    status = (
        f"[context: {_k(prompt_tokens)} / {_k(limit)} tokens "
        f"({pct:.0f}%). {rounds_elapsed} rounds elapsed.]"
    )
    if pct < 10:
        tier = "low"
    elif pct < 20:
        tier = "advisory"
    elif pct < 80:
        tier = "strong"
    else:
        tier = "forced"

    if tier == "advisory":
        status += (
            "\nConsider evict_tool_results to drop rounds whose findings "
            "you've already recorded."
        )
    elif tier == "strong":
        status += "\nYou must evict stale reads or finalize on your next turn."
    elif tier == "forced":
        status += (
            "\nYou must finalize now. Call record_check for every pending "
            "check or evict stale reads before calling more read_file."
        )
    return status, tier


def _execute_read_file(tool_call, attempt_dir, solution_dir):
    """Execute a read_file tool call, extracting the specified row/column range from a CSV."""
    import csv as csv_mod

    args = json.loads(tool_call.function.arguments)
    source = args.get("source", "")
    filename = args.get("filename", "")
    start_row = args.get("start_row")
    end_row = args.get("end_row")
    start_col = args.get("start_col")
    end_col = args.get("end_col")

    if source == "attempt":
        base_dir = attempt_dir
    elif source == "solution":
        base_dir = solution_dir
    else:
        return f"Error: Invalid source '{source}'. Use 'attempt' or 'solution'."

    if not base_dir or not Path(base_dir).exists():
        return f"Error: {source} directory not available."

    file_path = Path(base_dir) / filename
    if filename.endswith("_additional_format.txt"):
        return (
            f"Error: '{filename}' is not directly readable — its contents are "
            f"already inlined under the corresponding *_full.csv entry in the "
            f"file list."
        )
    if not file_path.exists():
        available = sorted(
            f.name
            for f in Path(base_dir).iterdir()
            if f.is_file() and not f.name.endswith("_additional_format.txt")
        )
        return (
            f"Error: File '{filename}' not found in {source} directory. "
            f"Available files: {', '.join(available)}"
        )

    # Prevent path traversal
    try:
        file_path.resolve().relative_to(Path(base_dir).resolve())
    except ValueError:
        return "Error: Invalid file path."

    # Validate range parameters
    if start_row is None or end_row is None or start_col is None or end_col is None:
        return (
            "Error: All range parameters are required: "
            "start_row, end_row, start_col, end_col."
        )

    try:
        start_row = int(start_row)
        end_row = int(end_row)
    except (TypeError, ValueError):
        return "Error: start_row and end_row must be integers."

    if start_row < 1 or end_row < start_row:
        return (
            f"Error: Invalid row range {start_row}-{end_row}. "
            f"Rows are 1-based and end_row must be >= start_row."
        )

    try:
        start_col_idx = _col_letter_to_index(start_col)
        end_col_idx = _col_letter_to_index(end_col)
    except (TypeError, AttributeError):
        return "Error: start_col and end_col must be column letters (e.g. 'A', 'Z')."

    if start_col_idx < 1 or end_col_idx < start_col_idx:
        return (
            f"Error: Invalid column range {start_col}-{end_col}. "
            f"end_col must be >= start_col."
        )

    num_rows = end_row - start_row + 1
    num_cols = end_col_idx - start_col_idx + 1
    cells = num_rows * num_cols
    if cells > READ_FILE_MAX_CELLS:
        return (
            f"Error: requested range is {cells} cells "
            f"({num_rows} rows x {num_cols} cols), which exceeds the "
            f"{READ_FILE_MAX_CELLS}-cell per-call limit. Split the request "
            f"into multiple smaller read_file calls."
        )

    try:
        with open(file_path, "r", encoding="utf-8") as f:
            reader = csv_mod.reader(f)
            all_rows = list(reader)
    except UnicodeDecodeError:
        return f"Error: File '{filename}' could not be read as text."

    total_rows = len(all_rows)
    total_cols = max((len(r) for r in all_rows), default=0)

    if start_row > total_rows:
        return f"Error: start_row {start_row} exceeds file row count ({total_rows})."

    # Clamp end_row to actual file size
    end_row = min(end_row, total_rows)

    # Extract the requested range (1-based to 0-based)
    extracted = []
    for row in all_rows[start_row - 1 : end_row]:
        # Slice columns (1-based col index to 0-based)
        row_slice = row[start_col_idx - 1 : end_col_idx]
        extracted.append(row_slice)

    # Format as CSV text
    import io

    output = io.StringIO()
    writer = csv_mod.writer(output)
    writer.writerows(extracted)
    result_text = output.getvalue()

    # Add a header with range info
    header = (
        f"[Range: rows {start_row}-{end_row}, columns {start_col}-{end_col} "
        f"| File: {total_rows} rows x {total_cols} cols]\n"
    )
    return header + result_text


def agentic_judge_case(
    task_folder: str,
    client: OpenAI,
    rubric_path: str,
    template_path: str,
    rubric_weight_path: str = None,
    model: str = JUDGE_MODEL,
    nocall: bool = False,
    noupload: bool = False,
    use_existing: bool = True,
    attempt_model: str = None,
    run_calculation: bool = False,
    cached_solution_csv_dir: str = None,
    cached_attempt_csv_dir: str = None,
    attempt_sheet_name_filter: bool = False,
    ignore_sheets: list[str] | None = None,
    carry_over_context: bool = True,
    max_tool_rounds: int = AGENTIC_JUDGE_MAX_ROUNDS,
    auto_routed: bool = False,
    reasoning_effort: str | None = None,
):
    """Execute the judging workflow using an agentic multi-turn approach.

    Unlike judge_case, this function:
    - Does not reduce context (no CSV shortening)
    - Builds prompts dynamically from rubric descriptions and file metadata
    - Uses multi-turn tool-calling so the judge LLM can query specific files
    - Optionally carries over findings between category evaluations

    Args:
        task_folder: Path to the task folder containing Excel files.
        client: Configured OpenRouter/OpenAI client.
        rubric_path: Path to the rubric JSON file.
        rubric_weight_path: Path to the rubric weights JSON file.
        model: Model identifier for API calls.
        nocall: If True, skip API calls (for testing).
        noupload: If True, skip file preparation (for testing).
        use_existing: If True, skip regenerating files if they already exist.
        attempt_model: Name of the AI model that generated the attempt.
        run_calculation: If True, run Excel formula calculations before extraction.
        cached_solution_csv_dir: Path to pre-extracted solution CSVs.
        cached_attempt_csv_dir: Path to pre-extracted attempt CSVs.
        attempt_sheet_name_filter: If True, filter attempt sheets by name prefix.
        carry_over_context: If True, include prior category findings in subsequent
            category prompts.
        max_tool_rounds: Maximum number of tool-calling rounds per category.
        auto_routed: True iff this run was auto-routed from ``judge_case`` because
            the standard judge would have overflowed the char budget. Recorded
            in ``_metadata.json`` and the result dict so downstream bookkeeping
            (DB writes, experiment analysis) can distinguish "agentic by config"
            from "agentic by overflow".

    Returns:
        dict: Same structure as judge_case — paths, scores, parse info.
    """
    # Shared preparation: validation, file processing
    prep = _prepare_case(
        task_folder=task_folder,
        rubric_path=rubric_path,
        rubric_weight_path=rubric_weight_path,
        use_existing=use_existing,
        run_calculation=run_calculation,
        cached_solution_csv_dir=cached_solution_csv_dir,
        cached_attempt_csv_dir=cached_attempt_csv_dir,
        attempt_sheet_name_filter=attempt_sheet_name_filter,
        ignore_sheets=ignore_sheets,
        agentic=True,
    )

    output_dir = prep["output_dir"]
    cache_log_path = prep["cache_log_path"]
    golden_solution_dir = prep["golden_solution_dir"]
    ai_attempt_dir = prep["ai_attempt_dir"]
    weights_data = prep["weights_data"]
    context_file_path = prep["context_file_path"]
    rubric_json_path = prep["rubric_json_path"]
    start_time = prep["start_time"]
    versions = prep["versions"]
    CHECK_ORDER = prep["CHECK_ORDER"]
    task_folder_name = prep["task_folder_name"]

    logger.info("=" * 80)
    logger.info("Agentic Judge Evaluation Workflow")
    logger.info("=" * 80)
    logger.info(
        f"Grading task: {task_folder_name}, model: {model}, "
        f"rubric: {versions['RUBRIC_VERSION']}, "
        f"judge version: {versions['JUDGE_VERSION']}"
    )
    logger.info("=" * 80)

    if noupload:
        logger.info("\n--noupload flag set. Skipping file preparation.")
        remove_log_file(cache_log_path)
        shutil.copy(cache_log_path, str(output_dir / "judge.log"))
        return

    # Gather available files (no shortening — use raw extracted CSVs)
    golden_solution_files = (
        prepare_directory_files(golden_solution_dir) if golden_solution_dir else {}
    )
    ai_attempt_files = prepare_directory_files(ai_attempt_dir) if ai_attempt_dir else {}

    # Exclude *_additional_format.txt — their content is already inlined under
    # the corresponding *_full.csv entry in the file metadata block.
    attempt_file_list = sorted(
        f for f in ai_attempt_files if not f.endswith("_additional_format.txt")
    )
    solution_file_list = sorted(
        f for f in golden_solution_files if not f.endswith("_additional_format.txt")
    )

    logger.info(f"\n  Attempt files: {attempt_file_list}")
    logger.info(f"  Solution files: {solution_file_list}")

    # Build file metadata (dimensions + additional_format.txt content)
    attempt_file_metadata = _build_file_metadata(ai_attempt_dir)
    solution_file_metadata = _build_file_metadata(golden_solution_dir)

    # Build context messages (.txt → inline text; .pdf / other → base64 image_url)
    context_messages = _build_agentic_context_messages(context_file_path)
    if context_file_path:
        logger.info(f"  Context file: {context_file_path.name}")

    # Build check name mapping
    check_name_mapping = build_check_name_mapping(str(rubric_json_path))

    if nocall:
        logger.info("\n--nocall flag set. Skipping API calls.")
        remove_log_file(cache_log_path)
        shutil.copy(cache_log_path, str(output_dir / "judge.log"))
        return

    # Agentic evaluation loop
    logger.info("\n[Agentic] Starting multi-turn evaluation...")

    all_responses = {}
    parse_failures = {}

    token_tracking = {
        "evaluations": {},
        "total_message_size": 0,
        "total_message_size_with_images": 0,
        "total_tokens": 0,
        "total_prompt_tokens": 0,
        "total_completion_tokens": 0,
        "total_cost": 0.0,
    }

    # Preload rubric for check-letter derivation per category
    with open(str(rubric_json_path), "r", encoding="utf-8") as _rf:
        _rubric_data = json.load(_rf)

    for stage_idx, category in enumerate(CHECK_ORDER):
        logger.info(f"\n  [Category] {category} (stage {stage_idx})...")

        rubric_checks_text = render_rubric_checks(str(rubric_json_path), category)
        num_checks = len(_rubric_data.get(category, []))
        check_letters = list(string.ascii_uppercase[:num_checks])

        compile_kwargs = dict(
            category=category,
            rubric_checks_text=rubric_checks_text,
            check_letters_text=", ".join(check_letters),
            attempt_files_text=_render_files_text(
                attempt_file_list, attempt_file_metadata
            ),
            solution_files_text=_render_files_text(
                solution_file_list, solution_file_metadata
            ),
            prior_findings=_render_prior_findings_block(
                json.dumps(all_responses, indent=2)
                if carry_over_context and all_responses
                else None
            ),
        )
        if context_messages:
            compile_kwargs["context_messages"] = context_messages

        stages = compile_prompt(template_path, **compile_kwargs)
        seed_messages = list(stages[0])

        state = AgenticCategoryLoop(category, check_letters, seed_messages)

        cumulative_metrics = {
            "message_size": 0,
            "message_size_with_images": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        }
        tool_call_stats: dict[str, int] = {
            t["function"]["name"]: 0 for t in AGENTIC_JUDGE_TOOLS
        }
        tool_call_args_log: list[dict] = []
        # Seed message chars are counted up-front; subsequent additions are
        # counted as they're appended so the running gap with
        # response.usage.prompt_tokens reflects eviction savings.
        for m in state.messages:
            size = _measure_message_chars(m)
            cumulative_metrics["message_size"] += size
            cumulative_metrics["message_size_with_images"] += size

        api_retries = 0
        max_api_retries = 5
        chars_per_token = 0.0  # calibrated from each real usage.prompt_tokens

        round_idx = 0
        while round_idx < max_tool_rounds:
            state.round = round_idx + 1
            logger.info(f"    Round {state.round}...")

            # Pressure signal: estimate the upcoming call's tokens directly
            # from current wire chars (calibrated via the most recent
            # response.usage). last_prompt_tokens alone lags by a turn —
            # it misses huge tool results appended between calls.
            wire_chars_pre = _wire_char_total(state.messages)
            wire_tokens_est = _estimate_wire_tokens(wire_chars_pre, chars_per_token)
            status, tier = _build_pressure_signal(
                wire_tokens_est,
                AGENTIC_JUDGE_CONTEXT_TOKEN_LIMIT,
                state.round - 1,
            )
            logger.info(f"      Pressure ({tier}): {status.splitlines()[0]}")
            pressure_msg = {"role": "user", "content": status}
            state.append_wire(pressure_msg, tag="pressure_note")
            psize = _measure_message_chars(pressure_msg)
            cumulative_metrics["message_size"] += psize
            cumulative_metrics["message_size_with_images"] += psize

            # Snapshot wire chars at the moment we actually send — this is
            # what response.usage.prompt_tokens will correspond to, so the
            # ratio we derive below is calibrated against the real call.
            wire_chars_at_call = _wire_char_total(state.messages)

            try:
                _msgs = state.messages
                if is_anthropic_model(model) or is_openai_model(model):
                    # OpenAI (direct) 400s on emf/wmf/bmp/tiff parts just like
                    # Anthropic; OpenRouter normalizes them, direct API doesn't.
                    _msgs = strip_unsupported_anthropic_images(_msgs)
                _create_kwargs = {
                    "model": to_api_model_id(model),
                    "messages": _msgs,
                    "tools": AGENTIC_JUDGE_TOOLS,
                }
                if reasoning_effort is not None:
                    # OpenAI chat/completions rejects function tools with any
                    # reasoning_effort except 'none' (gpt-5.5).
                    _create_kwargs["reasoning_effort"] = (
                        "none" if is_openai_model(model) else reasoning_effort
                    )
                response = client.chat.completions.create(**_create_kwargs)
            except Exception as e:
                err_str = str(e)
                # Context-length errors (400) are not retryable — the same
                # payload will fail again. Break out and let partial-failure
                # handling save what we've recorded so far.
                if "maximum context length" in err_str or (
                    "400" in err_str and "context" in err_str.lower()
                ):
                    logger.error(
                        f"    Context-length overflow (round {state.round}): {e}. "
                        f"Stopping category; partial judgement will be saved."
                    )
                    state.log_event(
                        "context_overflow",
                        error=err_str[:500],
                        wire_chars=wire_chars_at_call,
                    )
                    break
                api_retries += 1
                if api_retries > max_api_retries:
                    logger.error(
                        f"    Giving up after {max_api_retries} API retries: {e}"
                    )
                    break
                wait = min(2**api_retries + 1, 30)
                logger.warning(
                    f"    API error (round {state.round}, retry "
                    f"{api_retries}/{max_api_retries}): {e}. "
                    f"Retrying in {wait}s..."
                )
                time.sleep(wait)
                continue  # retry same round without advancing round_idx

            # OpenRouter sometimes wraps upstream provider errors in a 200 OK
            # envelope with choices=null + a top-level `error` field. The SDK
            # parses that without raising, so we have to detect it manually
            # and retry the same round.
            if not response.choices:
                err_detail = (
                    (response.model_extra or {}).get("error")
                    or getattr(response, "error", None)
                    or "no error field"
                )
                api_retries += 1
                if api_retries > max_api_retries:
                    logger.error(
                        f"    Giving up after {max_api_retries} retries: "
                        f"empty choices. err={err_detail}"
                    )
                    state.log_event(
                        "empty_choices_giveup",
                        error=str(err_detail)[:500],
                        wire_chars=wire_chars_at_call,
                    )
                    break
                wait = min(2**api_retries + 1, 30)
                logger.warning(
                    f"    Empty choices (round {state.round}, retry "
                    f"{api_retries}/{max_api_retries}): err={err_detail}. "
                    f"Retrying in {wait}s..."
                )
                state.log_event(
                    "empty_choices",
                    error=str(err_detail)[:500],
                    wire_chars=wire_chars_at_call,
                )
                time.sleep(wait)
                continue  # retry same round without advancing round_idx
            api_retries = 0

            usage = response.usage
            if usage:
                cumulative_metrics["prompt_tokens"] += usage.prompt_tokens or 0
                cumulative_metrics["completion_tokens"] += usage.completion_tokens or 0
                cumulative_metrics["total_tokens"] += usage.total_tokens or 0
                if usage.prompt_tokens:
                    state.last_prompt_tokens = usage.prompt_tokens
                    if wire_chars_at_call > 0:
                        chars_per_token = wire_chars_at_call / usage.prompt_tokens

            choice = response.choices[0]
            msg = choice.message
            msg_size = _measure_message_chars(msg)
            cumulative_metrics["message_size"] += msg_size
            cumulative_metrics["message_size_with_images"] += msg_size

            if msg.tool_calls:
                state.append_wire(msg, tag="model_tool_call")

                for tc in msg.tool_calls:
                    name = tc.function.name
                    args_preview = str(tc.function.arguments or "")[:200]
                    logger.info(f"      Tool call: {name}({args_preview})")
                    tool_call_stats[name] = tool_call_stats.get(name, 0) + 1
                    try:
                        _args_parsed = json.loads(tc.function.arguments or "{}")
                    except (json.JSONDecodeError, TypeError):
                        _args_parsed = {"_raw": tc.function.arguments}
                    tool_call_args_log.append(
                        {
                            "round": state.round,
                            "phase": "main",
                            "tool": name,
                            "tool_call_id": tc.id,
                            "arguments": _args_parsed,
                        }
                    )

                    if name == "read_file":
                        tool_result = _execute_read_file(
                            tc, ai_attempt_dir, golden_solution_dir
                        )
                    elif name == "evict_tool_results":
                        try:
                            args = json.loads(tc.function.arguments or "{}")
                            before_round = int(args.get("before_round", 0))
                        except (json.JSONDecodeError, ValueError, TypeError) as e:
                            tool_result = (
                                f"Error: invalid arguments for "
                                f"evict_tool_results: {e}"
                            )
                        else:
                            # Never evict the current round mid-iteration —
                            # doing so would orphan the tool_call that
                            # invoked evict_tool_results itself.
                            before_round = min(before_round, state.round)
                            tool_result = state.evict(before_round)
                    elif name in (
                        "record_check",
                        "append_mistake",
                        "get_working_judgement",
                    ):
                        tool_result = _execute_scratchpad_tool(tc, state.working)
                    else:
                        tool_result = f"Error: unknown tool '{name}'."

                    tool_msg = {
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": tool_result,
                    }
                    state.append_wire(tool_msg, tag="tool_result")
                    tsize = _measure_message_chars(tool_msg)
                    cumulative_metrics["message_size"] += tsize
                    cumulative_metrics["message_size_with_images"] += tsize

                round_idx += 1
                continue

            # No tool calls: apply finalization rules
            if msg.content:
                state.append_wire(msg, tag="assistant_text")

            if state.working.pending:
                missing = [
                    l for l in state.working.check_letters if l in state.working.pending
                ]
                nudge_content = (
                    f"You haven't recorded decisions for: "
                    f"{', '.join(missing)}. Call record_check for each "
                    f"before concluding."
                )
                nudge_msg = {"role": "user", "content": nudge_content}
                state.append_wire(nudge_msg, tag="nudge")
                nsize = _measure_message_chars(nudge_msg)
                cumulative_metrics["message_size"] += nsize
                cumulative_metrics["message_size_with_images"] += nsize
                logger.info(f"      Nudged: pending {missing}")
                round_idx += 1
                continue

            # No tool calls, no pending → done
            logger.info(
                f"      Model stopped with all checks recorded: "
                f"{state.working.coverage_str()}"
            )
            break

        # Exhausted max_tool_rounds with pending checks → force the model to
        # output its best-effort decisions for whatever it hasn't recorded.
        if round_idx >= max_tool_rounds and state.working.pending:
            missing_initial = [
                l for l in state.working.check_letters if l in state.working.pending
            ]
            logger.warning(
                f"    Max rounds ({max_tool_rounds}) exhausted with pending "
                f"checks {missing_initial}. Entering forced finalization."
            )
            state.log_event(
                "forced_finalization_start",
                pending=missing_initial,
                max_tool_rounds=max_tool_rounds,
            )

            force_msg_content = (
                f"You have exhausted the maximum number of tool-calling "
                f"rounds ({max_tool_rounds}) but still have pending checks: "
                f"{', '.join(missing_initial)}. You must now record your "
                f"pass/fail decisions for ALL remaining pending checks "
                f"immediately using record_check, based on the evidence you "
                f"have already gathered. No further file reads or evictions "
                f"are permitted; only record_check, append_mistake, and "
                f"get_working_judgement tools are available. Output your "
                f"best judgement now."
            )
            force_msg = {"role": "user", "content": force_msg_content}
            state.append_wire(force_msg, tag="forced_finalization")
            fsize = _measure_message_chars(force_msg)
            cumulative_metrics["message_size"] += fsize
            cumulative_metrics["message_size_with_images"] += fsize

            finalize_tools = [
                t
                for t in AGENTIC_JUDGE_TOOLS
                if t["function"]["name"]
                in ("record_check", "append_mistake", "get_working_judgement")
            ]

            max_forced_rounds = 5
            forced_round = 0
            while forced_round < max_forced_rounds and state.working.pending:
                forced_round += 1
                state.round = max_tool_rounds + forced_round
                logger.info(
                    f"    Forced finalization round "
                    f"{forced_round}/{max_forced_rounds}..."
                )

                try:
                    _msgs = state.messages
                    if is_anthropic_model(model) or is_openai_model(model):
                        _msgs = strip_unsupported_anthropic_images(_msgs)
                    _create_kwargs = {
                        "model": to_api_model_id(model),
                        "messages": _msgs,
                        "tools": finalize_tools,
                    }
                    if reasoning_effort is not None:
                        _create_kwargs["reasoning_effort"] = (
                            "none" if is_openai_model(model) else reasoning_effort
                        )
                    response = client.chat.completions.create(**_create_kwargs)
                except Exception as e:
                    logger.error(f"    Forced finalization API error: {e}. Stopping.")
                    state.log_event("forced_finalization_error", error=str(e)[:500])
                    break

                usage = response.usage
                if usage:
                    cumulative_metrics["prompt_tokens"] += usage.prompt_tokens or 0
                    cumulative_metrics["completion_tokens"] += (
                        usage.completion_tokens or 0
                    )
                    cumulative_metrics["total_tokens"] += usage.total_tokens or 0

                choice = response.choices[0]
                msg = choice.message
                msg_size = _measure_message_chars(msg)
                cumulative_metrics["message_size"] += msg_size
                cumulative_metrics["message_size_with_images"] += msg_size

                if msg.tool_calls:
                    state.append_wire(msg, tag="model_tool_call")
                    for tc in msg.tool_calls:
                        name = tc.function.name
                        args_preview = str(tc.function.arguments or "")[:200]
                        logger.info(f"      Tool call: {name}({args_preview})")
                        tool_call_stats[name] = tool_call_stats.get(name, 0) + 1
                        try:
                            _args_parsed = json.loads(tc.function.arguments or "{}")
                        except (json.JSONDecodeError, TypeError):
                            _args_parsed = {"_raw": tc.function.arguments}
                        tool_call_args_log.append(
                            {
                                "round": state.round,
                                "phase": "forced_finalization",
                                "tool": name,
                                "tool_call_id": tc.id,
                                "arguments": _args_parsed,
                            }
                        )
                        if name in (
                            "record_check",
                            "append_mistake",
                            "get_working_judgement",
                        ):
                            tool_result = _execute_scratchpad_tool(tc, state.working)
                        else:
                            tool_result = (
                                f"Error: tool '{name}' is disabled in forced "
                                f"finalization. Only record_check / "
                                f"append_mistake / get_working_judgement are "
                                f"allowed."
                            )
                        tool_msg = {
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "content": tool_result,
                        }
                        state.append_wire(tool_msg, tag="tool_result")
                        tsize = _measure_message_chars(tool_msg)
                        cumulative_metrics["message_size"] += tsize
                        cumulative_metrics["message_size_with_images"] += tsize
                    continue

                if msg.content:
                    state.append_wire(msg, tag="assistant_text")

                if state.working.pending:
                    missing_now = [
                        l
                        for l in state.working.check_letters
                        if l in state.working.pending
                    ]
                    nudge_content = (
                        f"You still have not recorded decisions for: "
                        f"{', '.join(missing_now)}. Call record_check for "
                        f"each pending check now."
                    )
                    nudge_msg = {"role": "user", "content": nudge_content}
                    state.append_wire(nudge_msg, tag="nudge")
                    nsize = _measure_message_chars(nudge_msg)
                    cumulative_metrics["message_size"] += nsize
                    cumulative_metrics["message_size_with_images"] += nsize
                else:
                    break

            state.log_event(
                "forced_finalization_end",
                pending_after=[
                    l for l in state.working.check_letters if l in state.working.pending
                ],
                forced_rounds_used=forced_round,
            )

        # Exited the loop — figure out why.
        if state.working.pending:
            missing = [
                l for l in state.working.check_letters if l in state.working.pending
            ]
            logger.warning(
                f"    WARNING: {category} finishing with pending checks: "
                f"{missing} (round_idx={round_idx}/{max_tool_rounds})"
            )
            parse_failures[category] = {
                "success": False,
                "count": len(missing),
                "responses": [f"Missing decisions for checks: {missing}"],
                "pending_checks": missing,
                "partial_recorded": [
                    l for l in state.working.check_letters if l in state.working.working
                ],
            }

        final_judgement = state.working.finalize()
        for item in final_judgement:
            letter = item.get("check")
            name = check_name_mapping.get((category, letter))
            if name:
                item["name"] = name

        state.append_synthetic_final(final_judgement)
        all_responses[category] = final_judgement

        # Track metrics
        token_tracking["evaluations"][category] = cumulative_metrics
        cost_info = calculate_cost(
            model,
            cumulative_metrics["prompt_tokens"],
            cumulative_metrics["completion_tokens"],
        )
        token_tracking["evaluations"][category]["cost"] = cost_info["total_cost"]
        token_tracking["evaluations"][category]["chars_per_token"] = (
            round(
                cumulative_metrics["message_size"]
                / cumulative_metrics["prompt_tokens"],
                2,
            )
            if cumulative_metrics["prompt_tokens"] > 0
            else 0
        )
        token_tracking["total_message_size"] += cumulative_metrics["message_size"]
        token_tracking["total_message_size_with_images"] += cumulative_metrics[
            "message_size_with_images"
        ]
        token_tracking["total_tokens"] += cumulative_metrics["total_tokens"]
        token_tracking["total_prompt_tokens"] += cumulative_metrics["prompt_tokens"]
        token_tracking["total_completion_tokens"] += cumulative_metrics[
            "completion_tokens"
        ]
        token_tracking["total_cost"] += cost_info["total_cost"]

        logger.info(
            f"    Tokens: {cumulative_metrics['prompt_tokens']:,} prompt + "
            f"{cumulative_metrics['completion_tokens']:,} completion | "
            f"Cost: ${cost_info['total_cost']:.6f}"
        )
        logger.info(f"    Final coverage: {state.working.coverage_str()}")

        # Persist the full transcript (append-only, including evicted
        # entries marked with _evicted_at_round and eviction events).
        conversation_logs_dir = output_dir / "judge_conversation_logs"
        conversation_logs_dir.mkdir(parents=True, exist_ok=True)
        conversation_path = (
            conversation_logs_dir / f"conversation_messages_{category}.json"
        )
        with open(conversation_path, "w", encoding="utf-8") as f:
            json.dump(state.transcript, f, indent=2)
        dump_messages_yaml(state.transcript, conversation_path.with_suffix(".yaml"))

        total_tool_calls = sum(tool_call_stats.values())
        if total_tool_calls:
            stats_str = ", ".join(
                f"{k}={v}" for k, v in sorted(tool_call_stats.items())
            )
        else:
            stats_str = "(none)"
        logger.info(f"    Tool calls: {total_tool_calls} total | {stats_str}")

        tool_calls_path = conversation_logs_dir / f"tool_calls_{category}.json"
        with open(tool_calls_path, "w", encoding="utf-8") as f:
            json.dump(
                {"stats": tool_call_stats, "calls": tool_call_args_log},
                f,
                indent=2,
            )

        logger.info(f"    {category} evaluation completed")
        time.sleep(0.5)

    # Shared finalization
    return _finalize_case(
        all_responses=all_responses,
        output_dir=output_dir,
        weights_data=weights_data,
        token_tracking=token_tracking,
        model=model,
        attempt_model=attempt_model,
        task_folder_name=task_folder_name,
        golden_solution_files=golden_solution_files,
        ai_attempt_files=ai_attempt_files,
        context_file_path=context_file_path,
        start_time=start_time,
        cache_log_path=cache_log_path,
        versions=versions,
        golden_solution_dir=golden_solution_dir,
        ai_attempt_dir=ai_attempt_dir,
        parse_failures=parse_failures,
        agentic=True,
        auto_routed=auto_routed,
        reasoning_effort=reasoning_effort,
    )


# ============================================================================
# CLI Entry Point
# ============================================================================


def main(args):
    """Main entry point that wires CLI args to judge_case or agentic_judge_case."""
    load_project_configs(verbose=True)

    # Resolve paths from config
    rubric_path = str(
        relative_path_from_project_root(
            load_env_var("JUDGE_RUBRIC", default="./prompts/rubrics/rubric_7.json")
        )
    )
    template_path = str(
        relative_path_from_project_root(
            load_env_var(
                "JUDGE_PROMPT_TEMPLATE", default="./prompts/judge_template_6_3.yaml"
            )
        )
    )
    agentic_template_path = str(
        relative_path_from_project_root(
            load_env_var(
                "AGENTIC_JUDGE_PROMPT_TEMPLATE",
                default="./prompts/agentic_judge_template_1.yaml",
            )
        )
    )
    rubric_weight_path = str(
        relative_path_from_project_root(
            load_env_var(
                "JUDGE_RUBRIC_WEIGHT",
                default="./prompts/rubrics/rubric_6_weights.json",
            )
        )
    )

    client = get_client(args.model)

    if args.agentic:
        agentic_judge_case(
            task_folder=args.folder_to_grade,
            client=client,
            rubric_path=rubric_path,
            template_path=agentic_template_path,
            rubric_weight_path=rubric_weight_path,
            model=args.model,
            nocall=args.nocall,
            noupload=args.noupload,
            use_existing=not args.no_use_existing,
            run_calculation=args.run_calculation,
            attempt_sheet_name_filter=args.attempt_sheet_name_filter,
            ignore_sheets=args.ignore_sheets,
            carry_over_context=args.carry_over_context,
            max_tool_rounds=args.max_tool_rounds,
            reasoning_effort=args.reasoning_effort,
        )
    else:
        judge_case(
            task_folder=args.folder_to_grade,
            client=client,
            rubric_path=rubric_path,
            template_path=template_path,
            rubric_weight_path=rubric_weight_path,
            model=args.model,
            nocall=args.nocall,
            noupload=args.noupload,
            use_existing=not args.no_use_existing,
            run_calculation=args.run_calculation,
            solution_context_char_limit=args.solution_char_limit,
            attempt_context_char_limit=args.attempt_char_limit,
            total_character_limit=args.total_char_limit,
            attempt_sheet_name_filter=args.attempt_sheet_name_filter,
            ignore_sheets=args.ignore_sheets,
            on_overflow=args.on_overflow,
            agentic_template_path=agentic_template_path,
            carry_over_context=args.carry_over_context,
            max_tool_rounds=args.max_tool_rounds,
            reasoning_effort=args.reasoning_effort,
        )


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Run local judge on specified attempt and solution files."
    )
    parser.add_argument(
        "-f",
        "--folder-to-grade",
        required=True,
        help="Path to folder containing student attempt, solution files, and possibly context files. "
        "Relative paths are interpreted from the project root directory.",
    )
    parser.add_argument(
        "-o",
        "--output-folder",
        required=False,
        default=None,
        help="Path to folder where feedback and scores will be written. "
        "Defaults to a 'judge_results' subfolder within the folder to grade.",
    )
    parser.add_argument(
        "--model",
        default=JUDGE_MODEL,
        help=f"OpenRouter model to use for grading (default: {JUDGE_MODEL})",
    )
    parser.add_argument(
        "--nocall",
        action="store_true",
        help="Skip API calls (for testing file preparation only)",
    )
    parser.add_argument(
        "--noupload",
        action="store_true",
        help="Skip file preparation (for testing file discovery only)",
    )
    parser.add_argument(
        "--no-use-existing",
        default=True,
        type=str2bool,
        help="Force re-extraction of CSV files even if they already exist",
    )
    parser.add_argument(
        "--run-calculation",
        action="store_true",
        help="Run Excel formula calculations via LibreOffice before extracting CSVs",
    )
    parser.add_argument(
        "--solution-char-limit",
        type=int,
        default=DEFAULT_SOLUTION_CONTEXT_CHAR_LIMIT,
        help=f"Character limit for golden solution context (default: {DEFAULT_SOLUTION_CONTEXT_CHAR_LIMIT:,})",
    )
    parser.add_argument(
        "--attempt-char-limit",
        type=int,
        default=DEFAULT_ATTEMPT_CONTEXT_CHAR_LIMIT,
        help=f"Character limit for AI attempt context (default: {DEFAULT_ATTEMPT_CONTEXT_CHAR_LIMIT:,})",
    )
    parser.add_argument(
        "--total-char-limit",
        type=int,
        default=DEFAULT_TOTAL_CHARACTER_LIMIT,
        help=f"Total character limit for combined solution + attempt (default: {DEFAULT_TOTAL_CHARACTER_LIMIT:,})",
    )
    parser.add_argument(
        "--attempt-sheet-name-filter",
        action="store_true",
        help="Filter attempt sheets to only include those starting with 'answers_' or 'model_', stripping the prefix",
    )
    parser.add_argument(
        "--ignore-sheets",
        nargs="+",
        default=None,
        help=(
            "Sheet names to drop from both attempt and solution before grading "
            "(case-insensitive, matched against the safe sheet name). "
            "Example: --ignore-sheets cover."
        ),
    )
    parser.add_argument(
        "--agentic",
        action="store_true",
        help="Use the agentic judge (multi-turn tool-calling) instead of the standard judge",
    )
    parser.add_argument(
        "--carry-over-context",
        action="store_true",
        default=True,
        help="(Agentic only) Carry over findings between category evaluations (default: True)",
    )
    parser.add_argument(
        "--no-carry-over-context",
        action="store_false",
        dest="carry_over_context",
        help="(Agentic only) Do not carry over findings between category evaluations",
    )
    parser.add_argument(
        "--max-tool-rounds",
        type=int,
        default=AGENTIC_JUDGE_MAX_ROUNDS,
        help=f"(Agentic only) Maximum number of tool-calling rounds per category (default: {AGENTIC_JUDGE_MAX_ROUNDS})",
    )
    parser.add_argument(
        "--on-overflow",
        choices=["route_to_agentic", "shorten"],
        default="route_to_agentic",
        help=(
            "(Standard judge only) What to do when extracted CSVs exceed the "
            "char budget. 'route_to_agentic' (default) hands off to the agentic "
            "judge with the unshortened CSVs as cached input. 'shorten' uses "
            "the legacy lossy CSV-shortening path."
        ),
    )
    parser.add_argument(
        "--reasoning-effort",
        type=str,
        default="minimal",
        choices=["none", "minimal", "low", "medium", "high"],
        help=(
            "Reasoning/thinking effort passed to the judge model "
            "(default: minimal). Models without thinking support may reject "
            "the kwarg."
        ),
    )

    # Args preprocessing
    args = parser.parse_args()

    args.folder_to_grade = get_absolute_path(args.folder_to_grade)
    if args.output_folder is not None:
        args.output_folder = get_absolute_path(args.output_folder)
    else:
        args.output_folder = f"{args.folder_to_grade}/judge_results"

    logger.info(
        f"Running local judge with parameters: {json.dumps(vars(args), indent=2)}"
    )

    main(args)
