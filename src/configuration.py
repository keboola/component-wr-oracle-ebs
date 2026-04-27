from enum import StrEnum

from keboola.component.exceptions import UserException
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator


class WriteMode(StrEnum):
    PRESET = "preset"
    CUSTOM = "custom"


class WriteOperation(StrEnum):
    INSERT = "insert"
    UPSERT = "upsert"


class SSHKeys(BaseModel):
    """Keypair payload emitted by the platform `ssh-editor` widget."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    private: str = Field(alias="#private", default="")
    public: str = ""


class SSHOptions(BaseModel):
    """Optional SSH tunnel for the Oracle DB connection.

    Rendered by the platform `ssh-editor` widget. Camel-case aliases match
    the keys emitted by that widget.
    """

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    enabled: bool = False
    user: str = ""
    ssh_host: str = Field(alias="sshHost", default="")
    ssh_port: int = Field(alias="sshPort", default=22)
    keys: SSHKeys = Field(default_factory=SSHKeys)


class DestinationConfig(BaseModel):
    """Target Oracle EBS write configuration."""

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
    submit_program: bool = True
    wait_for_completion: bool = True
    poll_interval: int = 10
    poll_timeout: int = 600


class Configuration(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    # DB connection fields — flattened from configSchema "connection" block
    db_host: str = ""
    db_port: int = 1521
    db_service_name: str = ""
    db_user: str = ""
    db_password: str = Field(alias="#db_password", default="")

    # ISG REST API connection (for TCA/AR writes)
    rest_url: str = ""
    rest_user: str = ""
    rest_password: str = Field(alias="#rest_password", default="")

    # Optional APPS-equivalent credential, used only by the FND_GLOBAL/FND_REQUEST block
    # in submit_concurrent_request. Falls back to db_user/db_password when unset.
    apps_user: str = ""
    apps_password: str = Field(alias="#apps_password", default="")

    # Default org_id for REST calls
    default_org_id: int = 204

    # Optional SSH tunnel for the DB connection
    ssh_options: SSHOptions = Field(default_factory=SSHOptions)

    # Row-level config
    destination: DestinationConfig = Field(default_factory=DestinationConfig)

    debug: bool = False

    @model_validator(mode="before")
    @classmethod
    def _flatten_connection(cls, data: dict) -> dict:
        if isinstance(data, dict) and "connection" in data:
            conn = data.pop("connection")
            if isinstance(conn, dict):
                data = {**data, **conn}
        if isinstance(data, dict) and "rest_connection" in data:
            rest_conn = data.pop("rest_connection")
            if isinstance(rest_conn, dict):
                # Drop the UI-only enable_rest flag before merging
                rest_conn = {k: v for k, v in rest_conn.items() if k != "enable_rest"}
                data = {**data, **rest_conn}
        if isinstance(data, dict) and "apps_connection" in data:
            apps_conn = data.pop("apps_connection")
            if isinstance(apps_conn, dict):
                # Drop the UI-only enable_apps_user flag before merging
                apps_conn = {k: v for k, v in apps_conn.items() if k != "enable_apps_user"}
                data = {**data, **apps_conn}
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

    def validate_rest_connection(self) -> None:
        if not self.rest_url:
            raise UserException("'rest_url' is required for REST API write operations.")
        if not self.rest_user:
            raise UserException("'rest_user' is required for REST API authentication.")
        if not self.rest_password:
            raise UserException("'rest_password' is required for REST API authentication.")

    def validate_apps_connection(self) -> None:
        if not self.apps_user:
            raise UserException("'apps_user' is required when 'Use Dedicated APPS-Equivalent User' is enabled.")
        if not self.apps_password:
            raise UserException("'apps_password' is required when 'Use Dedicated APPS-Equivalent User' is enabled.")

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
