from __future__ import annotations

from pydantic import BaseModel, Field


class DMail(BaseModel):
    message: str = Field(description="The message to send.")
    checkpoint_id: int = Field(description="The checkpoint to send the message back to.", ge=0)
    # TODO: allow restoring filesystem state to the checkpoint


class DenwaRenjiError(Exception):
    pass


class DenwaRenji:
    def __init__(self):
        self._pending_dmail: DMail | None = None
        self._n_checkpoints: int = 0

    def send_dmail(self, dmail: DMail):
        """Send a D-Mail. Intended to be called by the SendDMail tool."""
        if self._pending_dmail is not None:
            raise DenwaRenjiError("Only one D-Mail can be sent at a time")
        # Pydantic `ge=0` already rejects negative checkpoint_id; this check
        # rejects ids at or beyond the number of recorded checkpoints.
        if dmail.checkpoint_id >= self._n_checkpoints:
            raise DenwaRenjiError("There is no checkpoint with the given ID")
        self._pending_dmail = dmail

    def set_n_checkpoints(self, n_checkpoints: int):
        """Set the number of checkpoints. Intended to be called by the soul."""
        self._n_checkpoints = n_checkpoints

    def fetch_pending_dmail(self) -> DMail | None:
        """Fetch a pending D-Mail. Intended to be called by the soul."""
        pending_dmail = self._pending_dmail
        self._pending_dmail = None
        return pending_dmail
