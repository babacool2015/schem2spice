"""All tests run against the reference project (Marantz PM-80 Mk II).

Engine tests and the golden PM11 tests need no PDF. Tests that read the scan skip
when the project's pages are missing (put the PDF in the project's data/, then
`make ingest`)."""

from pathlib import Path

from schem2spice import project

REPO = Path(__file__).resolve().parents[1]
MARANTZ = project.use(REPO / "projects" / "marantz-pm80mk2")
