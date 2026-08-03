"""
WebAgent Abstract Base Class - Strategy Pattern for web-based AI agents.

This module defines the common interface that all web-based AI agents
(e.g., ClaudeWebAgent, ChatGPTWebAgent) must implement. The engine and
batch runner operate against this base class type, allowing new agents
to be added without modifying orchestration logic.

Classes:
    WebAgentState: Enum of possible agent states.
    ConversationMessage: Dataclass representing a single conversation turn.
    WebAgent: Abstract base class defining the agent interface.
"""

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional

logger = logging.getLogger(__name__)


class WebAgentState(Enum):
    """Possible states for a web-based AI agent interface."""
    RUNNING = "running"              # Agent is generating a response
    READY = "ready"                  # Ready to accept input
    RATE_LIMITED = "rate_limited"    # Hit rate limit
    AUTH_REQUIRED = "auth_required"  # Need to log in
    ERROR = "error"                  # Error state
    UNKNOWN = "unknown"             # State cannot be determined


@dataclass
class ConversationMessage:
    """A single message in the conversation."""
    role: str  # "user" or "assistant"
    content: str
    timestamp: Optional[datetime] = None
    metadata: dict = field(default_factory=dict)


class WebAgent(ABC):
    """
    Abstract base class for web-based AI agent automation.

    Defines the interface that all web agent implementations must follow.
    Concrete subclasses (e.g., ClaudeWebAgent, ChatGPTWebAgent) implement
    the abstract methods with provider-specific Playwright selectors and
    interaction logic.

    The engine and batch runner depend on this interface, enabling new
    agents to be added via the Strategy Pattern without changes to
    orchestration code.

    Args:
        page: Playwright page instance connected to the browser.
        config: Configuration dictionary for the task and agent.
        shutdown_event: Optional asyncio.Event for graceful shutdown signaling.
        completion_logger: Optional logger for timing and completion tracking.
    """

    def __init__(
        self,
        page,
        config: dict,
        shutdown_event=None,
        completion_logger=None,
    ):
        self.page = page
        self.config = config
        self.shutdown_event = shutdown_event
        self.completion_logger = completion_logger

        # Conversation state
        self.messages: list[ConversationMessage] = []
        self.current_response_count = 0

        # Set by download_all_artifacts(): whether download controls were
        # visible on the page at all. Lets the caller distinguish "the model
        # produced nothing" (a Continue prompt is legitimate) from "a file
        # exists but retrieval failed" (re-prompting spams a finished model).
        self.last_download_saw_buttons = False

    @abstractmethod
    async def navigate_to_new_chat(self) -> bool:
        """
        Navigate to the provider's chat interface to start a fresh conversation.

        Returns:
            True if navigation succeeded and the interface is ready.
        """
        ...

    @abstractmethod
    async def get_state(self) -> WebAgentState:
        """
        Determine the current state of the web interface.

        Returns:
            WebAgentState enum value representing the current state.
        """
        ...

    @abstractmethod
    async def upload_files(self, file_paths: list[str]) -> bool:
        """
        Upload files to the current conversation.

        Args:
            file_paths: List of local file paths to upload.

        Returns:
            True if all uploads succeeded.
        """
        ...

    @abstractmethod
    async def submit_prompt(self, prompt: str, prompt_number: int = 1) -> bool:
        """
        Submit a prompt to the AI agent.

        Args:
            prompt: The prompt text to submit.
            prompt_number: Sequence number of this prompt (for logging).

        Returns:
            True if submission succeeded.
        """
        ...

    @abstractmethod
    async def wait_for_response(self, prompt_number: int = 1) -> Optional[str]:
        """
        Wait for the AI agent to finish responding and extract the response.

        Args:
            prompt_number: Sequence number of the prompt being waited on.

        Returns:
            The response text, or None if failed or timed out.
        """
        ...

    @abstractmethod
    async def download_all_artifacts(self, download_dir: Optional[str] = None, timeout: int = 30000) -> list[str]:
        """
        Download all artifacts produced by the AI agent's response.

        Args:
            download_dir: Directory to save downloaded files. If None, uses
                browser default.
            timeout: Maximum time to wait for each download in milliseconds.

        Returns:
            List of paths to downloaded files.
        """
        ...

    @abstractmethod
    async def get_conversation_history(self) -> list[dict]:
        """
        Get the full conversation history.

        Returns:
            List of message dictionaries, each with at least 'role' and
            'content' keys.
        """
        ...

    @abstractmethod
    async def process_all_prompts(self, files_to_upload: list = None) -> bool:
        """
        Process all prompts from config sequentially.

        Orchestrates the full prompt loop: optionally uploads files, then
        iterates through each prompt in config, submitting and waiting for
        responses.

        Args:
            files_to_upload: Optional list of file paths to upload before
                the first prompt.

        Returns:
            True if all prompts completed successfully.
        """
        ...

    @abstractmethod
    async def ensure_features_enabled(self) -> bool:
        """
        Ensure all provider-specific features are enabled.

        For example, Claude may need Extended Thinking and Web Search enabled,
        while ChatGPT may need different features toggled.

        Returns:
            True if all required features are enabled.
        """
        ...
