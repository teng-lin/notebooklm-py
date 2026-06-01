"""Service helpers for skill install result handling."""

from collections.abc import Callable, Sequence


def report_mixed_no_clobber_up_to_date(
    emit: Callable[[str], None],
    *,
    skipped_up_to_date: Sequence[object],
    skipped_no_clobber: Sequence[object],
    installed_paths: Sequence[object],
    failed_targets: Sequence[object],
) -> None:
    """Report up-to-date targets when ``--no-clobber`` skipped other targets."""
    if skipped_up_to_date and skipped_no_clobber and not installed_paths and not failed_targets:
        emit(f"[green]Up to date[/green] {len(skipped_up_to_date)} target(s)")
