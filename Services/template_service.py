
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

TEMPLATE_DIR = (
    Path(__file__).resolve().parent.parent / "Email_Templates"
)

template_environment = Environment(
    loader=FileSystemLoader(str(TEMPLATE_DIR)),
    autoescape=select_autoescape(["html", "xml"]),
)


def render_email_template(template_name: str, **context) -> str:
    template = template_environment.get_template(template_name)
    return template.render(**context)
