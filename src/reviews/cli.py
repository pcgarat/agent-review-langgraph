from __future__ import annotations

import argparse
import sys

from reviews.decision import DecisionError, build_decision
from reviews.domain.models import DecisionOutcome, FindingDecision
from reviews.domain.paused import PausedReview
from reviews.paused_store import InMemoryPausedStore, RedisPausedStore
from reviews.queue import InMemoryReviewQueue, RedisReviewQueue, ReviewQueue
from reviews.settings import Settings, get_settings


def build_paused(settings: Settings) -> RedisPausedStore | InMemoryPausedStore:
    store = RedisPausedStore(url=settings.redis_url)
    if store.available():
        return store
    return InMemoryPausedStore()


def build_queue(settings: Settings) -> ReviewQueue:
    queue = RedisReviewQueue(url=settings.redis_url)
    if queue.available():
        return queue
    return InMemoryReviewQueue()


def _fmt_created(paused: PausedReview) -> str:
    return paused.created_at.strftime("%Y-%m-%d %H:%M")


def _cmd_list(settings: Settings) -> int:
    paused = build_paused(settings).all()
    if not paused:
        print("No hay reviews pausadas.")
        return 0
    for review in sorted(paused, key=lambda r: r.created_at):
        print(f"{review.review_key}  (pausada {_fmt_created(review)})")
        for pending in review.approval.pending:
            print(
                f"  - {pending.finding_id} [{pending.severity.value}] "
                f"{pending.proposed_action.value}: {pending.title} ({pending.file}:{pending.line})"
            )
        print(f"  decide: {review.approval.command_hint}")
    return 0


def _items_from_args(args: argparse.Namespace) -> list[FindingDecision]:
    items: list[FindingDecision] = []
    for finding_id in args.approve:
        items.append(FindingDecision(finding_id=finding_id, outcome=DecisionOutcome.APPROVED))
    for finding_id in args.reject:
        items.append(
            FindingDecision(finding_id=finding_id, outcome=DecisionOutcome.REJECTED, note=args.note)
        )
    for finding_id in args.noted:
        items.append(FindingDecision(finding_id=finding_id, outcome=DecisionOutcome.NOTED))
    return items


def _cmd_decide(settings: Settings, args: argparse.Namespace) -> int:
    store = build_paused(settings)
    paused = store.get(args.review_key)
    if paused is None:
        print(f"No hay una review pausada con clave {args.review_key}", file=sys.stderr)
        return 1
    try:
        decision = build_decision(
            paused, actor=args.actor, items=_items_from_args(args), reviewed_key=args.review_key
        )
    except DecisionError as exc:
        print(f"Decision rechazada: {exc}", file=sys.stderr)
        return 2

    # La decision viaja EN el job de reanudacion; el worker aplica el resume sobre
    # el mismo hilo. El CLI no construye el grafo ni toca el checkpoint.
    request = paused.request.model_copy(update={"decision": decision})
    build_queue(settings).enqueue(request)
    decided_at = decision.decided_at.strftime("%Y-%m-%d %H:%M")
    print(
        f"Decision encolada para {decision.review_key}: "
        f"{len(decision.items)} hallazgo(s), actor={decision.actor}, {decided_at}"
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="reviews", description="Canal de decision HITL")
    sub = parser.add_subparsers(dest="command", required=True)

    list_parser = sub.add_parser("list", help="Muestra las reviews pausadas y sus pendientes")
    list_parser.set_defaults(func=lambda settings, args: _cmd_list(settings))

    decide = sub.add_parser("decide", help="Emite una decision sobre una review pausada")
    decide.add_argument("review_key", help="Clave de la review pausada (ver `reviews list`)")
    decide.add_argument("--actor", required=True, help="Quien decide (queda auditado)")
    decide.add_argument(
        "--approve", action="append", default=[], metavar="ID", help="Autoriza el arreglo"
    )
    decide.add_argument(
        "--reject", action="append", default=[], metavar="ID", help="Descarta el hallazgo"
    )
    decide.add_argument(
        "--noted", action="append", default=[], metavar="ID", help="Se atiende fuera del agente"
    )
    decide.add_argument("--note", default="", help="Nota para los hallazgos rechazados")
    decide.set_defaults(func=_cmd_decide)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = get_settings()
    # `func` devuelve el codigo de salida; el `main` es el unico punto que
    # traduce un error de validacion a exit code.
    return int(args.func(settings, args))


if __name__ == "__main__":
    raise SystemExit(main())
