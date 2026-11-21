from decimal import Decimal, InvalidOperation

from django import template

from ..formatting import format_money

register = template.Library()


@register.simple_tag(takes_context=True)
def money(context, value):
    """Format an amount using the signed-in user's currency preferences."""
    try:
        value = Decimal(value)
    except (InvalidOperation, TypeError, ValueError):
        return ""
    return format_money(value, context.get("profile"))


@register.filter
def get_item(mapping, key):
    """``{{ mapping|get_item:key }}`` for dict lookups with a variable key."""
    return mapping.get(key, "") if hasattr(mapping, "get") else ""
