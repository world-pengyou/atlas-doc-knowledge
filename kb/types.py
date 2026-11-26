from dataclasses import dataclass


@dataclass
class ParsedBlock:
    text: str
    kind: str
    location: dict
    asset_path: str | None = None
    heading_path: tuple[str, ...] = ()
    heading_level: int = 0
    role: str = "body"
