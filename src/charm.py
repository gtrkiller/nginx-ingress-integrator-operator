#!/usr/bin/env python3
# Copyright 2021 Tom Haddon
# See LICENSE file for licensing details.

"""Charm the service.

Refer to the following post for a quick-start guide that will help you
develop a new k8s charm using the Operator Framework:

    https://discourse.charmhub.io/t/4208
"""

import logging
import os
from pathlib import Path

import kubernetes

# from kubernetes.client.rest import ApiException as K8sApiException

from ops.charm import CharmBase
from ops.framework import StoredState
from ops.main import main
from ops.model import (
    ActiveStatus,
    BlockedStatus,
)


logger = logging.getLogger(__name__)

REQUIRED_INGRESS_RELATION_FIELDS = {
    "service-hostname",
    "service-name",
    "service-port",
}

OPTIONAL_INGRESS_RELATION_FIELDS = {
    "max-body-size",
    "service-namespace",
    "session-cookie-max-age",
    "tls-secret-name",
}


def _core_v1_api():
    """Use the v1 k8s API."""
    cl = kubernetes.client.ApiClient()
    return kubernetes.client.CoreV1Api(cl)


def _networking_v1_beta1_api():
    """Use the v1 beta1 networking API."""
    return kubernetes.client.NetworkingV1beta1Api()


def _fix_lp_1892255():
    """Workaround for lp:1892255."""
    # Remove os.environ.update when lp:1892255 is FIX_RELEASED.
    os.environ.update(
        dict(e.split("=") for e in Path("/proc/1/environ").read_text().split("\x00") if "KUBERNETES_SERVICE" in e)
    )


class CharmK8SIngressCharm(CharmBase):
    """Charm the service."""

    _authed = False
    _stored = StoredState()

    def __init__(self, *args):
        super().__init__(*args)
        self.framework.observe(self.on.config_changed, self._on_config_changed)

        # ingress relation handling.
        self.framework.observe(self.on["ingress"].relation_changed, self._on_ingress_relation_changed)

        self.framework.observe(self.on.upgrade_charm, self._on_upgrade_charm)

        self._stored.set_default(ingress_relation_data=dict())

    def _ingress_relation_errors(self, ingress_data):
        """Confirm if we have any errors in our ingress relation.

        Return True if we have errors, or False if we don't."""
        missing_fields = sorted(
            [field for field in REQUIRED_INGRESS_RELATION_FIELDS if ingress_data.get(field) is None]
        )

        if missing_fields:
            logger.error("Missing required data fields for ingress relation: {}".format(", ".join(missing_fields)))
            self.unit.status = BlockedStatus("Missing fields for ingress: {}".format(", ".join(missing_fields)))
            return True
        return False

    def _get_ingress_relation_data(self):
        """Get ingress relation data, and add to StoredState."""
        for relations in self.model.relations.values():
            if relations:
                relation = relations[0]
                if len(relations) > 1:
                    logger.warning(
                        'Multiple relations of type "%s" detected,'
                        ' using only the first one (id: %s) for relation data.',
                        relation.name,
                        relation.id,
                    )
                if relation.name == "ingress":
                    ingress_data = dict(relation.data[relation.app].items())
                    if self._ingress_relation_errors(ingress_data):
                        return
                    self._stored.ingress_relation_data = ingress_data

    def _on_upgrade_charm(self, event):
        """Handle upgrade charm event."""
        # StoredState gets reset, so we need to resync the ingress relation
        # data. A config-changed event will be triggered after the upgrade-charm
        # to progress from there.
        self._get_ingress_relation_data()

    def _on_ingress_relation_changed(self, event):
        """Handle a change to the ingress relation."""
        if not self.unit.is_leader():
            return

        ingress_data = {
            field: event.relation.data[event.app].get(field)
            for field in REQUIRED_INGRESS_RELATION_FIELDS | OPTIONAL_INGRESS_RELATION_FIELDS
        }

        if self._ingress_relation_errors(ingress_data):
            return

        # Set our relation data to stored_state.
        self._stored.ingress_relation_data = ingress_data

        # Now trigger our config_changed handler.
        self._on_config_changed(event)

    @property
    def _k8s_service_name(self):
        """Return a service name for the use creating a k8s service."""
        # Avoid collision with service name created by Juju. Currently
        # Juju creates a K8s service listening on port 65535/TCP so we
        # need to create a separate one.
        return "{}-service".format(self._service_name)

    @property
    def _ingress_name(self):
        """Return an ingress name for use creating a k8s ingress."""
        # Follow the same naming convention as Juju.
        return "{}-ingress".format(
            self.config["service-name"] or self._stored.ingress_relation_data.get("service-name")
        )

    @property
    def _max_body_size(self):
        """Return the max-body-size to use for k8s ingress."""
        max_body_size = self.config["max-body-size"] or self._stored.ingress_relation_data.get("max-body-size")
        if max_body_size:
            return "{}m".format(max_body_size)
        # Don't return "0m" which would evaluate to True.
        return ""

    @property
    def _namespace(self):
        """Return the namespace to operate on."""
        return (
            self.config["service-namespace"]
            or self._stored.ingress_relation_data.get("service-namespace")
            or self.model.name
        )

    @property
    def _service_hostname(self):
        """Return the hostname for the service we're connecting to."""
        return self.config["service-hostname"] or self._stored.ingress_relation_data.get("service-hostname")

    @property
    def _service_name(self):
        """Return the name of the service we're connecting to."""
        return self.config["service-name"] or self._stored.ingress_relation_data.get("service-name")

    @property
    def _service_port(self):
        """Return the port for the service we're connecting to."""
        return self.config["service-port"] or int(self._stored.ingress_relation_data.get("service-port"))

    @property
    def _session_cookie_max_age(self):
        """Return the session-cookie-max-age to use for k8s ingress."""
        session_cookie_max_age = self.config["session-cookie-max-age"] or self._stored.ingress_relation_data.get(
            "session-cookie-max-age"
        )
        if session_cookie_max_age:
            return str(session_cookie_max_age)
        # Don't return "0" which would evaluate to True.
        return ""

    @property
    def _tls_secret_name(self):
        """Return the tls-secret-name to use for k8s ingress (if any)."""
        return self.config["tls-secret-name"] or self._stored.ingress_relation_data.get("tls-secret-name")

    def k8s_auth(self):
        """Authenticate to kubernetes."""
        if self._authed:
            return

        _fix_lp_1892255()

        # Work around for lp#1920102 - allow the user to pass in k8s config manually.
        if self.config["kube-config"]:
            with open('/kube-config', 'w') as kube_config:
                kube_config.write(self.config["kube-config"])
            kubernetes.config.load_kube_config(config_file='/kube-config')
        else:
            kubernetes.config.load_incluster_config()

        self._authed = True

    def _get_k8s_service(self):
        """Get a K8s service definition."""
        return kubernetes.client.V1Service(
            api_version="v1",
            kind="Service",
            metadata=kubernetes.client.V1ObjectMeta(name=self._k8s_service_name),
            spec=kubernetes.client.V1ServiceSpec(
                selector={"app.kubernetes.io/name": self._service_name},
                ports=[
                    kubernetes.client.V1ServicePort(
                        name="tcp-{}".format(self._service_port),
                        port=self._service_port,
                        target_port=self._service_port,
                    )
                ],
            ),
        )

    def _get_k8s_ingress(self):
        """Get a K8s ingress definition."""
        spec = kubernetes.client.NetworkingV1beta1IngressSpec(
            rules=[
                kubernetes.client.NetworkingV1beta1IngressRule(
                    host=self._service_hostname,
                    http=kubernetes.client.NetworkingV1beta1HTTPIngressRuleValue(
                        paths=[
                            kubernetes.client.NetworkingV1beta1HTTPIngressPath(
                                path="/",
                                backend=kubernetes.client.NetworkingV1beta1IngressBackend(
                                    service_port=self._service_port,
                                    service_name=self._k8s_service_name,
                                ),
                            )
                        ]
                    ),
                )
            ]
        )
        annotations = {
            "nginx.ingress.kubernetes.io/rewrite-target": "/",
        }
        if self._max_body_size:
            annotations["nginx.ingress.kubernetes.io/proxy-body-size"] = self._max_body_size
        if self._session_cookie_max_age:
            annotations["nginx.ingress.kubernetes.io/affinity"] = "cookie"
            annotations["nginx.ingress.kubernetes.io/affinity-mode"] = "balanced"
            annotations["nginx.ingress.kubernetes.io/session-cookie-change-on-failure"] = "true"
            annotations["nginx.ingress.kubernetes.io/session-cookie-max-age"] = self._session_cookie_max_age
            annotations["nginx.ingress.kubernetes.io/session-cookie-name"] = "{}_AFFINITY".format(
                self._service_name.upper()
            )
            annotations["nginx.ingress.kubernetes.io/session-cookie-samesite"] = "Lax"
        if self._tls_secret_name:
            spec.tls = kubernetes.client.NetworkingV1beta1IngressTLS(
                hosts=[self._service_hostname],
                secret_name=self._tls_secret_name,
            )
        else:
            annotations["nginx.ingress.kubernetes.io/ssl-redirect"] = "false"

        return kubernetes.client.NetworkingV1beta1Ingress(
            api_version="networking.k8s.io/v1beta1",
            kind="Ingress",
            metadata=kubernetes.client.V1ObjectMeta(
                name=self._ingress_name,
                annotations=annotations,
            ),
            spec=spec,
        )

    def _report_service_ips(self):
        """Report on service IP(s)."""
        self.k8s_auth()
        api = _core_v1_api()
        services = api.list_namespaced_service(namespace=self._namespace)
        return [x.spec.cluster_ip for x in services.items if x.metadata.name == self._k8s_service_name]

    def _define_service(self):
        """Create or update a service in kubernetes."""
        self.k8s_auth()
        api = _core_v1_api()
        body = self._get_k8s_service()
        services = api.list_namespaced_service(namespace=self._namespace)
        if self._k8s_service_name in [x.metadata.name for x in services.items]:
            api.patch_namespaced_service(
                name=self._k8s_service_name,
                namespace=self._namespace,
                body=body,
            )
            logger.info(
                "Service updated in namespace %s with name %s",
                self._namespace,
                self._service_name,
            )
        else:
            api.create_namespaced_service(
                namespace=self._namespace,
                body=body,
            )
            logger.info(
                "Service created in namespace %s with name %s",
                self._namespace,
                self._service_name,
            )

    def _define_ingress(self):
        """Create or update an ingress in kubernetes."""
        self.k8s_auth()
        api = _networking_v1_beta1_api()
        body = self._get_k8s_ingress()
        ingresses = api.list_namespaced_ingress(namespace=self._namespace)
        if self._ingress_name in [x.metadata.name for x in ingresses.items]:
            api.patch_namespaced_ingress(
                name=self._ingress_name,
                namespace=self._namespace,
                body=body,
            )
            logger.info(
                "Ingress updated in namespace %s with name %s",
                self._namespace,
                self._service_name,
            )
        else:
            api.create_namespaced_ingress(
                namespace=self._namespace,
                body=body,
            )
            logger.info(
                "Ingress created in namespace %s with name %s",
                self._namespace,
                self._service_name,
            )

    def _on_config_changed(self, _):
        """Handle the config changed event."""
        msg = ""
        # We only want to do anything here if we're the leader to avoid
        # collision if we've scaled out this application.
        if self.unit.is_leader() and self._service_name:
            self._define_service()
            self._define_ingress()
            # It's not recommended to do this via ActiveStatus, but we don't
            # have another way of reporting status yet.
            msg = "Ingress with service IP(s): {}".format(", ".join(self._report_service_ips()))
        self.unit.status = ActiveStatus(msg)


if __name__ == "__main__":  # pragma: no cover
    main(CharmK8SIngressCharm)
