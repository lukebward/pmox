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

    def cluster_nextid(self) -> Any:
        """Return the next free VMID from the cluster."""
        return self._api.cluster.nextid.get()

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

    def agent_network_interfaces(self, node: str, vmid) -> Any:
        """QEMU guest-agent network interfaces (needs the agent running in the guest).

        The hyphenated path segment ``network-get-interfaces`` isn't a valid Python
        identifier, so it's addressed via ``getattr`` (same as ``download-url``).
        """
        agent = self._guest(node, "qemu", vmid).agent
        return getattr(agent, "network-get-interfaces").get()

    def lxc_interfaces(self, node: str, vmid) -> list:
        """Network interfaces of a running LXC container (no guest agent needed)."""
        return self._guest(node, "lxc", vmid).interfaces.get()

    def update_config(self, node: str, kind: str, vmid, **params) -> Any:
        """Set/update guest options (synchronous PUT on the config endpoint)."""
        return self._guest(node, kind, vmid).config.put(**params)

    def resize_disk(self, node: str, kind: str, vmid, disk: str, size: str) -> Any:
        """Grow a disk. ``size`` is e.g. ``+10G`` (grow by) or ``50G`` (grow to)."""
        return self._guest(node, kind, vmid).resize.put(disk=disk, size=size)

    def guest_power(self, node: str, kind: str, vmid, action: str) -> Any:
        """``action`` in: start/stop/shutdown/reboot/suspend/resume/reset."""
        return getattr(self._guest(node, kind, vmid).status, action).post()

    def create_guest(self, node: str, kind: str, vmid, **params) -> Any:
        return getattr(self._api.nodes(node), kind).post(vmid=vmid, **params)

    def clone_guest(self, node: str, kind: str, vmid, newid, **params) -> Any:
        return self._guest(node, kind, vmid).clone.post(newid=newid, **params)

    def convert_to_template(self, node: str, kind: str, vmid) -> Any:
        """Convert a stopped guest into a template (one-way)."""
        return self._guest(node, kind, vmid).template.post()

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

    def list_appliances(self, node: str) -> list:
        """List downloadable appliance/container templates available to a node."""
        return self._api.nodes(node).aplinfo.get()

    def download_appliance(self, node: str, storage: str, template: str) -> Any:
        """Download a container template to a vztmpl storage (async; returns a UPID)."""
        return self._api.nodes(node).aplinfo.post(storage=storage, template=template)

    def download_url(
        self,
        node: str,
        storage: str,
        *,
        url: str,
        content: str,
        filename: str,
        checksum: Optional[str] = None,
        checksum_algorithm: Optional[str] = None,
    ) -> Any:
        """Fetch a URL into a storage (async; returns a UPID).

        ``content`` is ``import`` for VM disk images. The hyphenated REST path
        segment ``download-url`` and parameter ``checksum-algorithm`` are
        addressed via ``getattr``/dict expansion because they aren't valid
        Python identifiers.
        """
        endpoint = getattr(self._api.nodes(node).storage(storage), "download-url")
        params: dict = {"url": url, "content": content, "filename": filename}
        if checksum is not None:
            params["checksum"] = checksum
        if checksum_algorithm is not None:
            params["checksum-algorithm"] = checksum_algorithm
        return endpoint.post(**params)

    # ---- tasks ----
    def list_tasks(self, node: str, **params) -> list:
        return self._api.nodes(node).tasks.get(**params)

    def task_status(self, node: str, upid: str) -> dict:
        return self._api.nodes(node).tasks(upid).status.get()

    def task_log(self, node: str, upid: str, **params) -> list:
        return self._api.nodes(node).tasks(upid).log.get(**params)

    # ---- helpers ----
    def locate_guest(self, vmid) -> Optional[dict]:
        """The /cluster/resources row for a vmid (carries node, type, name), or None."""
        target = int(vmid)
        for r in self.cluster_resources(type="vm"):
            if int(r.get("vmid", -1)) == target:
                return r
        return None

    def resolve_node(self, vmid) -> Optional[str]:
        """Find which node a vmid lives on (via /cluster/resources)."""
        row = self.locate_guest(vmid)
        return row.get("node") if row else None

    def guest_kind(self, vmid) -> Optional[str]:
        """Return 'qemu' or 'lxc' for a vmid, or None if not found."""
        row = self.locate_guest(vmid)
        return row.get("type") if row else None
