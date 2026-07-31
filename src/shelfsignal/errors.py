class ShelfSignalError(RuntimeError):
    exit_code = 1


class AuthRequired(ShelfSignalError):
    exit_code = 3


class ShelfUnavailable(ShelfSignalError):
    exit_code = 4


class ContentContractUnavailable(ShelfSignalError):
    exit_code = 5


class ArticleBodyUnavailable(ShelfSignalError):
    exit_code = 1
