from dataclasses import dataclass


@dataclass
class ParsedBlock:
    text: str
    kind: str
    location: dict
    asset_path: str | None = None
