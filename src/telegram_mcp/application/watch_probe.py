"""Request-local batch metadata and durable, uncommitted source observations."""

from collections.abc import Awaitable, Callable
from functools import partial

from telegram_mcp.application.change_ports import PeerState
from telegram_mcp.application.incremental import IncrementalContext, ReadBudget
from telegram_mcp.domain.models import ToolError
from telegram_mcp.domain.watches import WatchPosition


class WatchProbe:
    def __init__(
        self,
        context: IncrementalContext,
        position: WatchPosition,
        budget: ReadBudget,
        authorize: Callable[[str], Awaitable[None]],
    ):
        self.context, self.position, self.budget = context, position, budget
        self.authorize = authorize
        self.fresh: dict[str, PeerState] = {}
        self.visited: set[str] = set()

    def eligible(self, cid: str) -> bool:
        return (
            cid.startswith("-100")
            and cid not in self.position.probe_fallback
            and self.position.sources[cid].phase in {"baseline", "idle"}
        )

    async def prepare(self, cid: str) -> None:
        if not self.eligible(cid):
            return
        selected = [key for key in self.position.remaining if self.eligible(key)][:100]
        allowed = []
        for key in selected:
            try:
                await self.budget.call(partial(self.authorize, key), page=False)
                allowed.append(key)
            except ToolError as exc:
                if exc.retryable:
                    raise
                self.position.errors[key] = exc.issue(key)
                self.position.remaining.remove(key)
                self.visited.add(key)
        if not allowed:
            return
        try:
            states = await self.budget.call(partial(self.context.backend.peer_states, allowed))
        except ToolError as exc:
            if exc.retryable:
                raise
            # A forbidden/missing peer can reject the entire vector. Use the
            # ordinary isolated reader for this group, within the same budget.
            self.position.probe_fallback.extend(allowed)
            return
        for key in allowed:
            current = states.get(key)
            if current is None:
                self.position.probe_fallback.append(key)
                continue
            self.fresh[key] = current
            self.visited.add(key)
            self.position.errors.pop(key, None)
            if not current.exportable:
                self.position.errors[key] = ToolError(
                    "EXPORT_PROTECTED", "Telegram restricts saving content from this chat."
                ).issue(key)
                self.position.remaining.remove(key)
                continue
            source = self.position.sources[key]
            if (
                source.phase == "idle"
                and self.position.revisions.get(key) == current.revision
                and not any(seen.requires_recheck for seen in source.seen.values())
            ):
                self.position.remaining.remove(key)
                continue
            self.position.probes[key] = current.revision
            source.upper_id = max(source.position, current.revision.top_message_id)
            source.date_to = self.context.clock.utc_now()
            if source.phase == "baseline":
                source.phase = "new"
            else:
                source.recheck_ids = sorted(source.seen)
                source.recheck_index = 0
                source.phase = "recheck"

    async def check_source(self, cid: str) -> None:
        self.visited.add(cid)
        if cid not in self.fresh:
            await self.budget.call(
                partial(self.context.backend.check_export_source, cid), page=False
            )

    def complete(self, cid: str) -> None:
        revision = self.position.probes.pop(cid, None)
        if revision is not None:
            self.position.revisions[cid] = revision
        else:
            # A fallback scan cannot certify the old marker for a newer revision.
            self.position.revisions.pop(cid, None)
