from behavior.models import AttemptPermit, AttemptRejected, AttemptsSnapshot
from channels.models import SessionRef


class SessionAttempts:
    def __init__(self) -> None:
        self._session: SessionRef | None = None
        self._attempts = 0

    def start_session(self, session: SessionRef) -> None:
        if session != self._session:
            self._session = session
            self._attempts = 0

    def admit(self, session: SessionRef, limit: int) -> AttemptPermit | AttemptRejected:
        if session != self._session:
            return AttemptRejected("stale_session")
        if self.exhausted(limit):
            return AttemptRejected("limit_reached")
        self._attempts += 1
        return AttemptPermit(session, self._attempts)

    def exhausted(self, limit: int) -> bool:
        return limit > 0 and self._attempts >= limit

    def snapshot(self) -> AttemptsSnapshot:
        return AttemptsSnapshot(self._session, self._attempts)
