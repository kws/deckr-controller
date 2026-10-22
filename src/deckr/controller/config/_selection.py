"""Controller config selection over an already validated local snapshot."""

from collections.abc import Iterable, Mapping

from deckr.controller.config._data import DeviceConfig


def select_device_config(
    configs: Iterable[DeviceConfig], *, fingerprint: str, labels: Mapping[str, str]
) -> DeviceConfig | None:
    candidates = [
        config
        for config in configs
        if config.enabled
        and config.match.fingerprint == fingerprint
        and all(labels.get(key) == value for key, value in config.match.labels.items())
    ]
    if not candidates:
        return None
    specificity = max(len(config.match.labels) for config in candidates)
    best = [config for config in candidates if len(config.match.labels) == specificity]
    if len(best) != 1:
        ids = ", ".join(sorted(config.id for config in best))
        raise ValueError(
            f"Ambiguous device config match for fingerprint {fingerprint!r} labels {dict(labels)!r}: {ids}"
        )
    return best[0]


def copy_config(config: DeviceConfig | None) -> DeviceConfig | None:
    """Public readers own their config copy; edits cannot mutate the stored view."""
    return config.model_copy(deep=True) if config is not None else None
