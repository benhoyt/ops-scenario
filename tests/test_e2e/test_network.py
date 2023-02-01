import pytest
from ops.charm import CharmBase
from ops.framework import Framework

from scenario.state import CharmSpec, NetworkSpec, State, event, network, relation


@pytest.fixture(scope="function")
def mycharm():
    class MyCharm(CharmBase):
        _call = None
        called = False

        def __init__(self, framework: Framework):
            super().__init__(framework)

            for evt in self.on.events().values():
                self.framework.observe(evt, self._on_event)

        def _on_event(self, event):
            if MyCharm._call:
                MyCharm.called = True
                MyCharm._call(self, event)

    return MyCharm


def test_ip_get(mycharm):
    mycharm._call = lambda *_: True

    def fetch_unit_address(charm: CharmBase):
        rel = charm.model.get_relation("metrics-endpoint")
        assert str(charm.model.get_binding(rel).network.bind_address) == "1.1.1.1"

    State(
        relations=[relation(endpoint="metrics-endpoint", interface="foo")],
        networks=[NetworkSpec("metrics-endpoint", bind_id=0, network=network())],
    ).run(
        event("update-status"),
        CharmSpec(
            mycharm,
            meta={
                "name": "foo",
                "requires": {"metrics-endpoint": {"interface": "foo"}},
            },
        ),
        post_event=fetch_unit_address,
    )
