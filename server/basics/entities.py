# File location: /server/basics/entities.py
from uuid import UUID

from pydantic import BaseModel as Base


class BaseEntity(Base):
    def dict(self, **kwargs):
        d = super().dict(**kwargs)

        for key, value in d.items():
            if type(value) == UUID:
                d[key] = str(value)

        return d
