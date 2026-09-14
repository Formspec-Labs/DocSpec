"""Source-catalog interruption and counting fixtures."""

from collections.abc import Iterator

from docspec.errors import IntegrityError


class KillAfter:
    """Wrap a catalog policy so its item stream dies after ``yields`` items.

    Stands in for the harness kill that ended two catalog-A builds: the
    process stops mid-stream with no chance to finish, and whatever the
    workspace had committed is all a resume can see. Everything else
    delegates to the wrapped policy, so the builder's identity checks see
    the real policy.
    """

    def __init__(self, policy: object, yields: int) -> None:
        self._policy = policy
        self._yields = yields
        self.computed = 0

    def __getattr__(self, name: str) -> object:
        return getattr(self._policy, name)

    def iter_items(self, inputs: object, workspace: object) -> Iterator[object]:
        for item in self._policy.iter_items(inputs, workspace):  # type: ignore[attr-defined]
            if self.computed >= self._yields:
                raise IntegrityError("injected kill mid-stream")
            self.computed += 1
            yield item


class CountItems:
    """Delegate to a policy and count how many items it computed."""

    def __init__(self, policy: object) -> None:
        self._policy = policy
        self.computed = 0

    def __getattr__(self, name: str) -> object:
        return getattr(self._policy, name)

    def iter_items(self, inputs: object, workspace: object) -> Iterator[object]:
        for item in self._policy.iter_items(inputs, workspace):  # type: ignore[attr-defined]
            self.computed += 1
            yield item
