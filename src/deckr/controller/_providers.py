from __future__ import annotations

import inspect
import logging
from typing import Any

from deckr.core.backplane import DeckrBackplane
from deckr.core.component import Component, ComponentManager
from deckr.core.providers import (
    DRIVER_ENTRYPOINT_GROUP,
    PLUGIN_HOST_ENTRYPOINT_GROUP,
    ActivationOrigin,
    DriverProviderContext,
    PluginHostProviderContext,
    ProviderNotConfigured,
    available_provider_names,
    load_provider_factory,
    resolve_provider_instance_specs,
)

from deckr.controller._config_document import DeckrConfigDocument

logger = logging.getLogger(__name__)


def available_driver_names() -> list[str]:
    return available_provider_names(DRIVER_ENTRYPOINT_GROUP)


def available_plugin_host_names() -> list[str]:
    return available_provider_names(PLUGIN_HOST_ENTRYPOINT_GROUP)


def _accepts_context(factory: object) -> bool:
    try:
        signature = inspect.signature(factory)
    except (TypeError, ValueError):
        return False
    return "context" in signature.parameters


def _invoke_factory(factory: object, *, context: object, legacy_kwargs: dict[str, Any]):
    if _accepts_context(factory):
        return factory(context=context)
    return factory(**legacy_kwargs)


async def activate_driver_providers(
    document: DeckrConfigDocument,
    backplane: DeckrBackplane,
    component_manager: ComponentManager,
) -> list[Component]:
    specs = resolve_provider_instance_specs(
        document.core_document,
        namespace_path="deckr.drivers",
        discovered_provider_ids=available_driver_names(),
    )
    components: list[Component] = []

    for spec in specs:
        try:
            factory = load_provider_factory(DRIVER_ENTRYPOINT_GROUP, spec.provider_id)
        except Exception as exc:
            if spec.activation_origin == ActivationOrigin.IMPLICIT:
                logger.info(
                    "Skipping implicit driver %s (provider=%s): %s",
                    spec.instance_id,
                    spec.provider_id,
                    exc,
                )
                continue
            raise RuntimeError(
                f"Configured driver provider {spec.provider_id!r} failed to load"
            ) from exc

        if factory is None:
            if spec.activation_origin == ActivationOrigin.IMPLICIT:
                continue
            raise RuntimeError(
                f"Configured driver provider {spec.provider_id!r} is not installed"
            )

        context = DriverProviderContext(
            instance_id=spec.instance_id,
            provider_id=spec.provider_id,
            activation_origin=spec.activation_origin,
            raw_config=spec.raw_config,
            document=document.core_document,
            base_dir=document.base_dir,
            backplane=backplane,
        )
        try:
            component = _invoke_factory(
                factory,
                context=context,
                legacy_kwargs={
                    "activation_origin": spec.activation_origin,
                    "backplane": backplane,
                    "config": dict(spec.raw_config),
                    "document": document.core_document,
                    "event_bus": backplane.hardware_events,
                    "instance_id": spec.instance_id,
                    "provider_id": spec.provider_id,
                },
            )
        except ProviderNotConfigured:
            logger.info(
                "Skipping %s driver %s: provider reported inactive or not configured",
                spec.activation_origin.value,
                spec.instance_id,
            )
            continue

        if not isinstance(component, Component):
            raise TypeError(
                f"Driver provider {spec.provider_id!r} did not return a Component"
            )
        await component_manager.add_component(component)
        components.append(component)

    return components


async def activate_plugin_host_providers(
    document: DeckrConfigDocument,
    backplane: DeckrBackplane,
    component_manager: ComponentManager,
    *,
    controller_id: str,
) -> list[Component]:
    specs = resolve_provider_instance_specs(
        document.core_document,
        namespace_path="deckr.plugin_hosts",
        discovered_provider_ids=available_plugin_host_names(),
    )
    components: list[Component] = []

    for spec in specs:
        try:
            factory = load_provider_factory(
                PLUGIN_HOST_ENTRYPOINT_GROUP, spec.provider_id
            )
        except Exception as exc:
            if spec.activation_origin == ActivationOrigin.IMPLICIT:
                logger.info(
                    "Skipping implicit plugin host %s (provider=%s): %s",
                    spec.instance_id,
                    spec.provider_id,
                    exc,
                )
                continue
            raise RuntimeError(
                f"Configured plugin host provider {spec.provider_id!r} failed to load"
            ) from exc

        if factory is None:
            if spec.activation_origin == ActivationOrigin.IMPLICIT:
                continue
            raise RuntimeError(
                f"Configured plugin host provider {spec.provider_id!r} is not installed"
            )

        context = PluginHostProviderContext(
            instance_id=spec.instance_id,
            provider_id=spec.provider_id,
            activation_origin=spec.activation_origin,
            raw_config=spec.raw_config,
            document=document.core_document,
            base_dir=document.base_dir,
            backplane=backplane,
            controller_id=controller_id,
        )
        try:
            component = _invoke_factory(
                factory,
                context=context,
                legacy_kwargs={
                    "activation_origin": spec.activation_origin,
                    "backplane": backplane,
                    "config": dict(spec.raw_config),
                    "config_base_dir": document.base_dir,
                    "controller_id": controller_id,
                    "document": document.core_document,
                    "event_bus": backplane.plugin_messages,
                    "instance_id": spec.instance_id,
                    "name": spec.instance_id,
                    "plugin_configs": document.children("plugins"),
                    "provider_id": spec.provider_id,
                },
            )
        except ProviderNotConfigured:
            logger.info(
                "Skipping %s plugin host %s: provider reported inactive or not configured",
                spec.activation_origin.value,
                spec.instance_id,
            )
            continue

        if not isinstance(component, Component):
            raise TypeError(
                f"Plugin host provider {spec.provider_id!r} did not return a Component"
            )
        await component_manager.add_component(component)
        components.append(component)

    return components
