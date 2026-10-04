"""Bound synchronous broker calls without retrying any financial request."""

from collections import namedtuple
import grpc

CallDetails = namedtuple(
    "CallDetails", "method timeout metadata credentials wait_for_ready compression"
)


class DeadlineInterceptor(grpc.UnaryUnaryClientInterceptor):
    def __init__(self, timeout: float = 15):
        self.timeout = timeout

    def intercept_unary_unary(self, continuation, details, request):
        timeout = (
            self.timeout
            if details.timeout is None
            else min(details.timeout, self.timeout)
        )
        bounded = CallDetails(
            details.method,
            timeout,
            details.metadata,
            details.credentials,
            getattr(details, "wait_for_ready", None),
            getattr(details, "compression", None),
        )
        return continuation(bounded, request)
