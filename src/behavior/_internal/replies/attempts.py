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

    def admit(self, session: SessionRef, limit: int, *, input_ref: str, expires_at: float) -> AttemptPermit | AttemptRejected:
        if session != self._session:
            return AttemptRejected("stale_session")
        if self.exhausted(limit):
            return AttemptRejected("limit_reached")
        permit = AttemptPermit(session, self._attempts + 1, input_ref, expires_at)
        self._attempts += 1
        return permit

    def exhausted(self, limit: int) -> bool:
        if type(limit) is not int or limit < 0:
            raise ValueError("尝试限额须为非负整数；0表示不限额")
        return limit > 0 and self._attempts >= limit

    def snapshot(self) -> AttemptsSnapshot:
        return AttemptsSnapshot(self._session, self._attempts)
