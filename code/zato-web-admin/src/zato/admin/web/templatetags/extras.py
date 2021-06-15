# -*- coding: utf-8 -*-

"""
Copyright (C) Zato Source s.r.o. https://zato.io

Licensed under LGPLv3, see LICENSE.txt for terms and conditions.
"""

# Django
from django import template
from django.utils.safestring import mark_safe

# ################################################################################################################################

register = template.Library()

# ################################################################################################################################

# Taken from https://djangosnippets.org/snippets/38/ and slightly updated

@register.filter
def bunchget(obj, args):
    """ Try to get an attribute from an object.

    Example: {% if block|bunchget:"editable,True" %}

    Beware that the default is always a string, if you want this
    to return False, pass an empty second argument:
    {% if block|bunchget:"editable," %}
    """
    args = str(args).split(',')
    if len(args) == 1:
        (attribute, default) = [args[0], '']
    else:
        (attribute, default) = args

    if attribute in obj:
        return obj[attribute]

    return default

# ################################################################################################################################

# Taken from https://stackoverflow.com/a/16609498

@register.simple_tag
def url_replace(request, field, value):
    dict_ = request.GET.copy()
    dict_[field] = value

    return dict_.urlencode()

# ################################################################################################################################

@register.filter
def no_value_indicator(value):
    return value or mark_safe('<span class="form_hint">---</span>')

# ################################################################################################################################

@register.filter
def format_float(value, digits=5):

    if not value:
        return 0

    value = str(value)
    as_float = float(value)
    as_int = int(as_float)

    if as_int == as_float:
        result = as_int
    else:
        result = round(as_float, digits)

    result = str(result)

    return result

# ################################################################################################################################
