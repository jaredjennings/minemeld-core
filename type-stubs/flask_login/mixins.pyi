from ._compat import PY2 as PY2, text_type as text_type
from _typeshed import Incomplete

class UserMixin:
    __hash__: Incomplete
    @property
    def is_active(self): ...
    @property
    def is_authenticated(self): ...
    @property
    def is_anonymous(self): ...
    def get_id(self): ...
    def __eq__(self, other): ...
    def __ne__(self, other): ...

class AnonymousUserMixin:
    @property
    def is_authenticated(self): ...
    @property
    def is_active(self): ...
    @property
    def is_anonymous(self): ...
    def get_id(self) -> None: ...
