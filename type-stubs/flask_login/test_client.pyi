from flask.testing import FlaskClient

class FlaskLoginClient(FlaskClient):
    def __init__(self, *args, **kwargs) -> None: ...
