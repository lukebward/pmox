"""A thin, testable wrapper around the Proxmox VE API.

The underlying ``proxmoxer.ProxmoxAPI`` object is injected, so this class can be
unit-tested with a mock and constructed for real via :meth:`ProxmoxClient.from_settings`.
``proxmoxer`` is imported lazily inside ``from_settings`` so the rest of the
package (and the test-suite) does not require it to be installed.

Guest ``kind`` is the Proxmox API path segment: ``"qemu"`` for VMs, ``"lxc"`` for
containers.
"""

from __future__ import annotations

from typing import Any, Optional


class ProxmoxClient:
    def __init__(self, api: Any):
        self._api = api

    @classmethod
    def from_settings(cls, settings) -> "ProxmoxClient":
        from proxmoxer import ProxmoxAPI

        api = ProxmoxAPI(
            settings.host,
            user=settings.user,
            token_name=settings.token_name,
            token_value=settings.token_secret,
            verify_ssl=settings.verify_ssl,
            port=settings.port,
            timeout=settings.timeout,
            service="PVE",
        )
        return cls(api)

    # ---- version / cluster ----
    def version(self) -> dict:
        return self._api.version.get()

    def cluster_status(self) -> list:
        return self._api.cluster.status.get()

    def cluster_resources(self, type: Optional[str] = None) -> list:
        if type:
            return self._api.cluster.resources.get(type=type)
        return self._api.cluster.resources.get()

    # ---- nodes ----
    def list_nodes(self) -> list:
        return self._api.nodes.get()

    def node_status(self, node: str) -> dict:
        return self._api.nodes(node).status.get()

    # ---- guests (qemu / lxc) ----
    def _guest(self, node: str, kind: str, vmid):
        return getattr(self._api.nodes(node), kind)(vmid)

    def list_guests(self, kind: str, node: Optional[str] = None) -> list:
        """List VMs or containers cluster-wide (optionally filtered to one node)."""
        wanted = "qemu" if kind == "qemu" else "lxc"
        rows = [r for r in self.cluster_resources(type="vm") if r.get("type") == wanted]
        if node:
            rows = [r for r in rows if r.get("node") == node]
        return rows

    def guest_status(self, node: str, kind: str, vmid) -> dict:
        return self._guest(node, kind, vmid).status.current.get()

    def guest_config(self, node: str, kind: str, vmid) -> dict:
        return self._guest(node, kind, vmid).config.get()

    def guest_power(self, node: str, kind: str, vmid, action: str) -> Any:
        """``action`` in: start/stop/shutdown/reboot/suspend/resume/reset."""
        return getattr(self._guest(node, kind, vmid).status, action).post()

    def create_guest(self, node: str, kind: str, vmid, **params) -> Any:
        return getattr(self._api.nodes(node), kind).post(vmid=vmid, **params)

    def clone_guest(self, node: str, kind: str, vmid, newid, **params) -> Any:
        return self._guest(node, kind, vmid).clone.post(newid=newid, **params)

    def migrate_guest(self, node: str, kind: str, vmid, target, **params) -> Any:
        return self._guest(node, kind, vmid).migrate.post(target=target, **params)

    def delete_guest(self, node: str, kind: str, vmid, purge: bool = False) -> Any:
        if purge:
            return self._guest(node, kind, vmid).delete(purge=1)
        return self._guest(node, kind, vmid).delete()

    # ---- snapshots ----
    def list_snapshots(self, node: str, kind: str, vmid) -> list:
        return self._guest(node, kind, vmid).snapshot.get()

    def create_snapshot(self, node: str, kind: str, vmid, name: str, **params) -> Any:
        return self._guest(node, kind, vmid).snapshot.post(snapname=name, **params)

    def delete_snapshot(self, node: str, kind: str, vmid, name: str) -> Any:
        return self._guest(node, kind, vmid).snapshot(name).delete()

    def rollback_snapshot(self, node: str, kind: str, vmid, name: str) -> Any:
        return self._guest(node, kind, vmid).snapshot(name).rollback.post()

    # ---- storage ----
    def list_storage(self, node: Optional[str] = None) -> list:
        if node:
            return self._api.nodes(node).storage.get()
        return self._api.storage.get()

    def storage_content(self, node: str, storage: str) -> list:
        return self._api.nodes(node).storage(storage).content.get()

    # ---- tasks ----
    def list_tasks(self, node: str, **params) -> list:
        return self._api.nodes(node).tasks.get(**params)

    def task_status(self, node: str, upid: str) -> dict:
        return self._api.nodes(node).tasks(upid).status.get()

    def task_log(self, node: str, upid: str, **params) -> list:
        return self._api.nodes(node).tasks(upid).log.get(**params)

    # ---- helpers ----
    def resolve_node(self, vmid) -> Optional[str]:
        """Find which node a vmid lives on (via /cluster/resources)."""
        target = int(vmid)
        for r in self.cluster_resources(type="vm"):
            if int(r.get("vmid", -1)) == target:
                return r.get("node")
        return None

    def guest_kind(self, vmid) -> Optional[str]:
        """Return 'qemu' or 'lxc' for a vmid, or None if not found."""
        target = int(vmid)
        for r in self.cluster_resources(type="vm"):
            if int(r.get("vmid", -1)) == target:
                return r.get("type")
        return None
