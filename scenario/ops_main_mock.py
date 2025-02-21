#!/usr/bin/env python3
# Copyright 2023 Canonical Ltd.
# See LICENSE file for licensing details.
import inspect
import os
from typing import TYPE_CHECKING, Any, Sequence

import ops.charm
import ops.framework
import ops.model
import ops.storage
from ops import CharmBase
from ops.charm import CharmMeta
from ops.log import setup_root_logging

# use logger from ops.main so that juju_log will be triggered
from ops.main import CHARM_STATE_FILE, _Dispatcher, _get_charm_dir, _get_event_args
from ops.main import logger as ops_logger

if TYPE_CHECKING:
    from scenario.context import Context
    from scenario.state import Event, State, _CharmSpec


class NoObserverError(RuntimeError):
    """Error raised when the event being dispatched has no registered observers."""


class BadOwnerPath(RuntimeError):
    """Error raised when the owner path does not lead to a valid ObjectEvents instance."""


def _get_owner(root: Any, path: Sequence[str]) -> ops.ObjectEvents:
    """Walk path on root to an ObjectEvents instance."""
    obj = root
    for step in path:
        try:
            obj = getattr(obj, step)
        except AttributeError:
            raise BadOwnerPath(
                f"event_owner_path {path!r} invalid: {step!r} leads to nowhere.",
            )
    if not isinstance(obj, ops.ObjectEvents):
        raise BadOwnerPath(
            f"event_owner_path {path!r} invalid: does not lead to "
            f"an ObjectEvents instance.",
        )
    return obj


def _emit_charm_event(
    charm: "CharmBase",
    event_name: str,
    event: "Event" = None,
):
    """Emits a charm event based on a Juju event name.

    Args:
        charm: A charm instance to emit an event from.
        event_name: A Juju event name to emit on a charm.
        event_owner_path: Event source lookup path.
    """
    owner = _get_owner(charm, event.owner_path) if event else charm.on

    try:
        event_to_emit = getattr(owner, event_name)
    except AttributeError:
        ops_logger.debug("Event %s not defined for %s.", event_name, charm)
        raise NoObserverError(
            f"Cannot fire {event_name!r} on {owner}: "
            f"invalid event (not on charm.on). "
            f"Use Context.run_custom instead.",
        )

    args, kwargs = _get_event_args(charm, event_to_emit)
    ops_logger.debug("Emitting Juju event %s.", event_name)
    event_to_emit.emit(*args, **kwargs)


def setup_framework(
    charm_dir,
    state: "State",
    event: "Event",
    context: "Context",
    charm_spec: "_CharmSpec",
):
    from scenario.mocking import _MockModelBackend

    model_backend = _MockModelBackend(  # pyright: reportPrivateUsage=false
        state=state,
        event=event,
        context=context,
        charm_spec=charm_spec,
    )
    debug = "JUJU_DEBUG" in os.environ
    setup_root_logging(model_backend, debug=debug)
    ops_logger.debug(
        "Operator Framework %s up and running.",
        ops.__version__,
    )  # type:ignore

    metadata = (charm_dir / "metadata.yaml").read_text()
    actions_meta = charm_dir / "actions.yaml"
    if actions_meta.exists():
        actions_metadata = actions_meta.read_text()
    else:
        actions_metadata = None

    meta = CharmMeta.from_yaml(metadata, actions_metadata)
    model = ops.model.Model(meta, model_backend)

    charm_state_path = charm_dir / CHARM_STATE_FILE

    # TODO: add use_juju_for_storage support
    store = ops.storage.SQLiteStorage(charm_state_path)
    framework = ops.framework.Framework(store, charm_dir, meta, model)
    framework.set_breakpointhook()
    return framework


def setup_charm(charm_class, framework, dispatcher):
    sig = inspect.signature(charm_class)
    sig.bind(framework)  # signature check

    charm = charm_class(framework)
    dispatcher.ensure_event_links(charm)
    return charm


def setup(state: "State", event: "Event", context: "Context", charm_spec: "_CharmSpec"):
    """Setup dispatcher, framework and charm objects."""
    charm_class = charm_spec.charm_type
    charm_dir = _get_charm_dir()

    dispatcher = _Dispatcher(charm_dir)
    dispatcher.run_any_legacy_hook()

    framework = setup_framework(charm_dir, state, event, context, charm_spec)
    charm = setup_charm(charm_class, framework, dispatcher)
    return dispatcher, framework, charm


class Ops:
    """Class to manage stepping through ops setup, event emission and framework commit."""

    def __init__(
        self,
        state: "State",
        event: "Event",
        context: "Context",
        charm_spec: "_CharmSpec",
    ):
        self.state = state
        self.event = event
        self.context = context
        self.charm_spec = charm_spec

        # set by setup()
        self.dispatcher = None
        self.framework = None
        self.charm = None

        self._has_setup = False
        self._has_emitted = False
        self._has_committed = False

    def setup(self):
        """Setup framework, charm and dispatcher."""
        self._has_setup = True
        self.dispatcher, self.framework, self.charm = setup(
            self.state,
            self.event,
            self.context,
            self.charm_spec,
        )

    def emit(self):
        """Emit the event on the charm."""
        if not self._has_setup:
            raise RuntimeError("should .setup() before you .emit()")
        self._has_emitted = True

        try:
            if not self.dispatcher.is_restricted_context():
                self.framework.reemit()

            _emit_charm_event(self.charm, self.dispatcher.event_name, self.event)

        except Exception:
            self.framework.close()
            raise

    def commit(self):
        """Commit the framework and teardown."""
        if not self._has_emitted:
            raise RuntimeError("should .emit() before you .commit()")

        # emit collect-status events
        ops.charm._evaluate_status(self.charm)

        self._has_committed = True

        try:
            self.framework.commit()
        finally:
            self.framework.close()

    def finalize(self):
        """Step through all non-manually-called procedures and run them."""
        if not self._has_setup:
            self.setup()
        if not self._has_emitted:
            self.emit()
        if not self._has_committed:
            self.commit()
