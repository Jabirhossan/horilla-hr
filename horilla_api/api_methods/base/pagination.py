"""Shared pagination for the API's hand-rolled list views."""

from rest_framework.pagination import PageNumberPagination


class HorillaPageNumberPagination(PageNumberPagination):
    """
    Page-number pagination that actually honours ``?page_size=``.

    Every list view here instantiates its paginator by hand rather than going
    through DRF's generic machinery, and each one used a stock
    ``PageNumberPagination``. That class has no ``page_size_query_param``, so
    ``?page_size=`` was silently ignored and every page was 20 rows -- despite
    the Swagger parameter list advertising it (``horilla_api/docs.py``). A
    client wanting 50 rows had to make three round trips and could not tell
    why.

    ``max_page_size`` is what stops the parameter becoming a cheap way to ask
    the server to serialize an entire table.

    Defaults are unchanged: omit the parameter and you still get
    ``REST_FRAMEWORK["PAGE_SIZE"]`` rows, so this is additive for existing
    callers.
    """

    page_size_query_param = "page_size"
    max_page_size = 100
