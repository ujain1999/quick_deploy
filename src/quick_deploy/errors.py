"""Error type and exit codes shared by all commands."""

OK = 0
ERROR = 1
USAGE = 2
UNREACHABLE = 3  # deployed, but the public URL did not come up in time
NOT_FOUND = 4  # no deployment with that name


class QDError(Exception):
    def __init__(self, message: str, code: int = ERROR):
        super().__init__(message)
        self.code = code
