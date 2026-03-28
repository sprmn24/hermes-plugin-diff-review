"""Diff Review plugin — registration."""

from . import schemas, tools


def register(ctx):
    ctx.register_tool(
        name="diff_review",
        toolset="diff_review",
        schema=schemas.DIFF_REVIEW,
        handler=tools.diff_review,
    )
