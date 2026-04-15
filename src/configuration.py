from enum import StrEnum

from keboola.component.exceptions import UserException
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator


class WriteMode(StrEnum):
    PRESET = "preset"
    CUSTOM = "custom"


class WriteOperation(StrEnum):
    INSERT = "insert"
    UPSERT = "upsert"


class SourceConfig(BaseModel):
    """Input table configuration (Keboola Storage → component)."""

    input_table: str = ""


class DestinationConfig(BaseModel):
    """Target Oracle EBS table configuration."""

    write_mode: WriteMode = WriteMode.PRESET
    module: str = ""
    entity: str = ""
    org_id: int | None = None
    ledger_id: int | None = None
    target_table: str = ""
    write_operation: WriteOperation = WriteOperation.INSERT
    primary_key: list[str] = Field(default_factory=list)
    column_mapping: dict[str, str] = Field(default_factory=dict)
    batch_size: int = 1000


class Configuration(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    # Connection fields — flattened from configSchema "connection" block
    db_host: str = ""
    db_port: int = 1521
    db_service_name: str = ""
    db_user: str = Field(alias="#db_user", default="")
    db_password: str = Field(alias="#db_password", default="")

    # Row-level config
    source: SourceConfig = Field(default_factory=SourceConfig)
    destination: DestinationConfig = Field(default_factory=DestinationConfig)

    debug: bool = False

    @model_validator(mode="before")
    @classmethod
    def _flatten_connection(cls, data: dict) -> dict:
        if isinstance(data, dict) and "connection" in data:
            conn = data.pop("connection")
            if isinstance(conn, dict):
                data = {**data, **conn}
        return data

    def __init__(self, **data: object) -> None:
        try:
            super().__init__(**data)
        except ValidationError as e:
            error_messages = [f"{'.'.join(str(loc) for loc in err['loc'])}: {err['msg']}" for err in e.errors()]
            raise UserException(f"Configuration error: {', '.join(error_messages)}")

    def validate_connection(self) -> None:
        if not self.db_host:
            raise UserException("'db_host' is required in connection settings.")
        if not self.db_service_name:
            raise UserException("'db_service_name' is required in connection settings.")
        if not self.db_user:
            raise UserException("'db_user' is required in connection settings.")
        if not self.db_password:
            raise UserException("'db_password' is required in connection settings.")

    def validate_row(self) -> None:
        self.validate_connection()
        dest = self.destination
        if dest.write_mode == WriteMode.PRESET:
            if not dest.module:
                raise UserException("'module' is required in preset mode.")
            if not dest.entity:
                raise UserException("'entity' is required in preset mode.")
        else:
            if not dest.target_table:
                raise UserException("'target_table' is required in custom mode.")
        if dest.write_operation == WriteOperation.UPSERT and not dest.primary_key:
            raise UserException("'primary_key' is required for upsert operation.")
