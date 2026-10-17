"""Application errors translated to HTTP only by the presentation layer."""


class NotFound(Exception):
    pass


class Conflict(Exception):
    def __init__(self, reason='conflict'):
        self.reason = reason
        super().__init__(reason)
