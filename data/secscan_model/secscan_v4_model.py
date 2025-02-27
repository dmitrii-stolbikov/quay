import logging
import functools
import itertools
import random

from collections import namedtuple
from datetime import datetime, timedelta
from math import log10
from peewee import fn, JOIN
from enum import Enum

from data.secscan_model.interface import SecurityScannerInterface, InvalidConfigurationException
from data.secscan_model.datatypes import (
    ScanLookupStatus,
    SecurityInformationLookupResult,
    SecurityInformation,
    Feature,
    Layer,
    Metadata,
    NVD,
    CVSSv3,
    Vulnerability,
    PaginatedNotificationResult,
    PaginatedNotificationStatus,
    UpdatedVulnerability,
)
from data.readreplica import ReadOnlyModeException
from data.registry_model.datatypes import Manifest as ManifestDataType
from data.registry_model import registry_model
from util.migrate.allocator import yield_random_entries
from util.secscan.validator import V4SecurityConfigValidator
from util.secscan.v4.api import ClairSecurityScannerAPI, APIRequestFailure, InvalidContentSent
from util.secscan import PRIORITY_LEVELS, get_priority_from_cvssscore, fetch_vuln_severity
from util.secscan.blob import BlobURLRetriever
from util.config import URLSchemeAndHostname

from data.database import (
    Manifest,
    ManifestSecurityStatus,
    IndexerVersion,
    IndexStatus,
    Repository,
    User,
    db_transaction,
)

logger = logging.getLogger(__name__)


DEFAULT_SECURITY_SCANNER_V4_REINDEX_THRESHOLD = 86400  # 1 day


IndexReportState = namedtuple("IndexReportState", ["Index_Finished", "Index_Error"])(  # type: ignore[call-arg]
    "IndexFinished", "IndexError"
)


class ScanToken(namedtuple("NextScanToken", ["min_id"])):
    """
    ScanToken represents an opaque token that can be passed between runs of the security worker
    to continue scanning whereever the previous run left off. Note that the data of the token is
    *opaque* to the security worker, and the security worker should *not* pull any data out or modify
    the token in any way.
    """


class NoopV4SecurityScanner(SecurityScannerInterface):
    """
    No-op implementation of the security scanner interface for Clair V4.
    """

    def load_security_information(self, manifest_or_legacy_image, include_vulnerabilities=False):
        return SecurityInformationLookupResult.for_request_error("security scanner misconfigured")

    def perform_indexing(self, start_token=None, batch_size=None):
        return None

    def perform_indexing_recent_manifests(self, batch_size=None):
        return None

    def register_model_cleanup_callbacks(self, data_model_config):
        pass

    @property
    def legacy_api_handler(self):
        raise NotImplementedError("Unsupported for this security scanner version")

    def lookup_notification_page(self, notification_id, page_index=None):
        return None

    def process_notification_page(self, page_result):
        raise NotImplementedError("Unsupported for this security scanner version")

    def mark_notification_handled(self, notification_id):
        raise NotImplementedError("Unsupported for this security scanner version")


class V4SecurityScanner(SecurityScannerInterface):
    """
    Implementation of the security scanner interface for Clair V4 API-compatible implementations.
    """

    def __init__(self, app, instance_keys, storage):
        self.app = app
        self.storage = storage

        if app.config.get("SECURITY_SCANNER_V4_ENDPOINT", None) is None:
            raise InvalidConfigurationException(
                "Missing SECURITY_SCANNER_V4_ENDPOINT configuration"
            )

        validator = V4SecurityConfigValidator(
            app.config.get("FEATURE_SECURITY_SCANNER", False),
            app.config.get("SECURITY_SCANNER_V4_ENDPOINT", None),
        )

        if not validator.valid():
            msg = "Failed to validate security scanner V4 configuration"
            logger.warning(msg)
            raise InvalidConfigurationException(msg)

        self._secscan_api = ClairSecurityScannerAPI(
            endpoint=app.config.get("SECURITY_SCANNER_V4_ENDPOINT"),
            client=app.config.get("HTTPCLIENT"),
            blob_url_retriever=BlobURLRetriever(storage, instance_keys, app),
            jwt_psk=app.config.get("SECURITY_SCANNER_V4_PSK", None),
        )

    def load_security_information(self, manifest_or_legacy_image, include_vulnerabilities=False):
        if not isinstance(manifest_or_legacy_image, ManifestDataType):
            return SecurityInformationLookupResult.with_status(
                ScanLookupStatus.UNSUPPORTED_FOR_INDEXING
            )

        status = None
        try:
            status = ManifestSecurityStatus.get(manifest=manifest_or_legacy_image._db_id)
        except ManifestSecurityStatus.DoesNotExist:
            return SecurityInformationLookupResult.with_status(ScanLookupStatus.NOT_YET_INDEXED)

        if status.index_status == IndexStatus.FAILED:
            return SecurityInformationLookupResult.with_status(ScanLookupStatus.FAILED_TO_INDEX)

        if status.index_status == IndexStatus.MANIFEST_UNSUPPORTED:
            return SecurityInformationLookupResult.with_status(
                ScanLookupStatus.UNSUPPORTED_FOR_INDEXING
            )

        if status.index_status == IndexStatus.IN_PROGRESS:
            return SecurityInformationLookupResult.with_status(ScanLookupStatus.NOT_YET_INDEXED)

        assert status.index_status == IndexStatus.COMPLETED

        try:
            report = self._secscan_api.vulnerability_report(manifest_or_legacy_image.digest)
        except APIRequestFailure as arf:
            return SecurityInformationLookupResult.for_request_error(str(arf))

        if report is None:
            return SecurityInformationLookupResult.with_status(ScanLookupStatus.NOT_YET_INDEXED)

        # TODO(alecmerdler): Provide a way to indicate the current scan is outdated (`report.state != status.indexer_hash`)

        return SecurityInformationLookupResult.for_data(
            SecurityInformation(Layer(report["manifest_hash"], "", "", 4, features_for(report)))
        )

    def _get_manifest_iterator(
        self, indexer_state, min_id, max_id, batch_size=None, reindex_threshold=None
    ):
        # TODO(alecmerdler): Filter out any `Manifests` that are still being uploaded
        def not_indexed_query():
            return (
                Manifest.select(Manifest, ManifestSecurityStatus)
                .join(ManifestSecurityStatus, JOIN.LEFT_OUTER)
                .where(ManifestSecurityStatus.id >> None)
            )

        def index_error_query():
            return (
                Manifest.select(Manifest, ManifestSecurityStatus)
                .join(ManifestSecurityStatus)
                .where(
                    ManifestSecurityStatus.index_status == IndexStatus.FAILED,
                    ManifestSecurityStatus.last_indexed < reindex_threshold
                    or DEFAULT_SECURITY_SCANNER_V4_REINDEX_THRESHOLD,
                )
            )

        def needs_reindexing_query(indexer_hash):
            return (
                Manifest.select(Manifest, ManifestSecurityStatus)
                .join(ManifestSecurityStatus)
                .where(
                    ManifestSecurityStatus.index_status != IndexStatus.MANIFEST_UNSUPPORTED,
                    ManifestSecurityStatus.indexer_hash != indexer_hash,
                    ManifestSecurityStatus.last_indexed < reindex_threshold
                    or DEFAULT_SECURITY_SCANNER_V4_REINDEX_THRESHOLD,
                )
            )

        # 4^log10(total) gives us a scalable batch size into the billions.
        if not batch_size:
            batch_size = int(4 ** log10(max(10, max_id - min_id)))

        iterator = itertools.chain(
            yield_random_entries(
                not_indexed_query,
                Manifest.id,
                batch_size,
                max_id,
                min_id,
            ),
            yield_random_entries(
                index_error_query,
                Manifest.id,
                batch_size,
                max_id,
                min_id,
            ),
            yield_random_entries(
                lambda: needs_reindexing_query(indexer_state.get("state", "")),
                Manifest.id,
                batch_size,
                max_id,
                min_id,
            ),
        )

        return iterator

    def perform_indexing_recent_manifests(self, batch_size=None):
        try:
            indexer_state = self._secscan_api.state()
        except APIRequestFailure:
            return None

        if not batch_size:
            batch_size = self.app.config.get("SECURITY_SCANNER_V4_BATCH_SIZE", 0)

        reindex_threshold = datetime.utcnow() - timedelta(
            seconds=self.app.config.get("SECURITY_SCANNER_V4_REINDEX_THRESHOLD", 86400)
        )

        end_index = Manifest.select(fn.Max(Manifest.id)).scalar()
        start_index = max(end_index - batch_size, 1)

        iterator = self._get_manifest_iterator(
            indexer_state,
            start_index,
            end_index,
            batch_size=max(batch_size // 20, 1),
            reindex_threshold=reindex_threshold,
        )

        self._index(iterator, reindex_threshold)

    def perform_indexing(self, start_token=None, batch_size=None):
        try:
            indexer_state = self._secscan_api.state()
        except APIRequestFailure:
            return None

        if not batch_size:
            batch_size = self.app.config.get("SECURITY_SCANNER_V4_BATCH_SIZE", 0)

        reindex_threshold = datetime.utcnow() - timedelta(
            seconds=self.app.config.get("SECURITY_SCANNER_V4_REINDEX_THRESHOLD", 86400)
        )

        max_id = Manifest.select(fn.Max(Manifest.id)).scalar()

        start_index = (
            start_token.min_id
            if start_token is not None
            else Manifest.select(fn.Min(Manifest.id)).scalar()
        )

        if max_id is None or start_index is None or start_index > max_id:
            return None

        iterator = self._get_manifest_iterator(
            indexer_state,
            start_index,
            max_id,
            batch_size=batch_size,
            reindex_threshold=reindex_threshold,
        )

        self._index(iterator, reindex_threshold)

        return ScanToken(max_id + 1)

    def _index(self, iterator, reindex_threshold):
        def mark_manifest_unsupported(manifest):
            with db_transaction():
                ManifestSecurityStatus.delete().where(
                    ManifestSecurityStatus.manifest == manifest._db_id,
                    ManifestSecurityStatus.repository == manifest.repository._db_id,
                ).execute()
                ManifestSecurityStatus.create(
                    manifest=manifest._db_id,
                    repository=manifest.repository._db_id,
                    index_status=IndexStatus.MANIFEST_UNSUPPORTED,
                    indexer_hash="none",
                    indexer_version=IndexerVersion.V4,
                    metadata_json={},
                )

        def should_skip_indexing(manifest_candidate):
            """Check whether this manifest was preempted by another worker.
            That would be the case if the manifest references a manifestsecuritystatus,
            or if the reindex threshold is no longer valid.
            """
            if getattr(manifest_candidate, "manifestsecuritystatus", None):
                return manifest_candidate.manifestsecuritystatus.last_indexed >= reindex_threshold

            return len(manifest_candidate.manifestsecuritystatus_set) > 0

        for candidate, abt, num_remaining in iterator:
            manifest = ManifestDataType.for_manifest(candidate, None)
            if manifest.is_manifest_list:
                mark_manifest_unsupported(manifest)
                continue

            layers = registry_model.list_manifest_layers(manifest, self.storage, True)
            if layers is None or len(layers) == 0:
                logger.warning(
                    "Cannot index %s/%s@%s due to manifest being invalid (manifest has no layers)"
                    % (
                        candidate.repository.namespace_user,
                        candidate.repository.name,
                        manifest.digest,
                    )
                )
                mark_manifest_unsupported(manifest)
                continue

            if should_skip_indexing(candidate):
                logger.debug("Another worker preempted this worker")
                abt.set()
                continue

            logger.debug(
                "Indexing manifest [%d] %s/%s@%s"
                % (
                    manifest._db_id,
                    candidate.repository.namespace_user,
                    candidate.repository.name,
                    manifest.digest,
                )
            )

            try:
                (report, state) = self._secscan_api.index(manifest, layers)
            except InvalidContentSent as ex:
                mark_manifest_unsupported(manifest)
                logger.exception("Failed to perform indexing, invalid content sent")
                continue
            except APIRequestFailure as ex:
                logger.exception("Failed to perform indexing, security scanner API error")
                continue

            if report["state"] == IndexReportState.Index_Finished:
                index_status = IndexStatus.COMPLETED
            elif report["state"] == IndexReportState.Index_Error:
                index_status = IndexStatus.FAILED
            else:
                # Unknown state don't save anything
                continue

            with db_transaction():
                ManifestSecurityStatus.delete().where(
                    ManifestSecurityStatus.manifest == candidate
                ).execute()
                ManifestSecurityStatus.create(
                    manifest=candidate,
                    repository=candidate.repository,
                    error_json=report["err"],
                    index_status=index_status,
                    indexer_hash=state,
                    indexer_version=IndexerVersion.V4,
                    metadata_json={},
                )

    def lookup_notification_page(self, notification_id, page_index=None):
        try:
            notification_page_results = self._secscan_api.retrieve_notification_page(
                notification_id, page_index
            )

            # If we get back None, then the notification no longer exists.
            if notification_page_results is None:
                return PaginatedNotificationResult(
                    PaginatedNotificationStatus.FATAL_ERROR, None, None
                )
        except APIRequestFailure:
            return PaginatedNotificationResult(
                PaginatedNotificationStatus.RETRYABLE_ERROR, None, None
            )

        # FIXME(alecmerdler): Debugging tests failing in CI
        return PaginatedNotificationResult(
            PaginatedNotificationStatus.SUCCESS,
            notification_page_results["notifications"],
            notification_page_results.get("page", {}).get("next"),
        )

    def mark_notification_handled(self, notification_id):
        try:
            self._secscan_api.delete_notification(notification_id)
            return True
        except APIRequestFailure:
            return False

    def process_notification_page(self, page_result):
        for notification_data in page_result:
            if notification_data["reason"] != "added":
                continue

            yield UpdatedVulnerability(
                notification_data["manifest"],
                Vulnerability(
                    Severity=notification_data["vulnerability"].get("normalized_severity"),
                    Description=notification_data["vulnerability"].get("description"),
                    NamespaceName=notification_data["vulnerability"].get("package", {}).get("name"),
                    Name=notification_data["vulnerability"].get("name"),
                    FixedBy=notification_data["vulnerability"].get("fixed_in_version"),
                    Link=notification_data["vulnerability"].get("links"),
                    Metadata={},
                ),
            )

    def register_model_cleanup_callbacks(self, data_model_config):
        pass

    @property
    def legacy_api_handler(self):
        raise NotImplementedError("Unsupported for this security scanner version")


def features_for(report):
    """
    Transforms a Clair v4 `VulnerabilityReport` dict into the standard shape of a
    Quay Security scanner response.
    """
    features = []
    dedupe_vulns = {}
    for pkg_id, pkg in report["packages"].items():
        pkg_env = report["environments"][pkg_id][0]
        pkg_vulns = []
        # Quay doesn't care about vulnerabilities reported from different
        # repos so dedupe them. Key = package_name + package_version + vuln_name.
        for vuln_id in report["package_vulnerabilities"].get(pkg_id, []):
            vuln_key = (
                pkg["name"]
                + "_"
                + pkg["version"]
                + "_"
                + report["vulnerabilities"][vuln_id].get("name", "")
            )
            if not dedupe_vulns.get(vuln_key, False):
                pkg_vulns.append(report["vulnerabilities"][vuln_id])
            dedupe_vulns[vuln_key] = True

        enrichments = (
            {
                key: sorted(val, key=lambda x: x["baseScore"], reverse=True)[0]
                for key, val in list(report["enrichments"].values())[0][0].items()
            }
            if report.get("enrichments", {})
            else {}
        )

        features.append(
            Feature(
                pkg["name"],
                "",
                "",
                pkg_env["introduced_in"],
                pkg["version"],
                [
                    Vulnerability(
                        fetch_vuln_severity(vuln, enrichments),
                        vuln["updater"],
                        vuln["links"],
                        vuln["fixed_in_version"] if vuln["fixed_in_version"] != "0" else "",
                        vuln["description"],
                        vuln["name"],
                        Metadata(
                            vuln["updater"],
                            vuln.get("repository", {}).get("name"),
                            vuln.get("repository", {}).get("uri"),
                            vuln.get("distribution", {}).get("name"),
                            vuln.get("distribution", {}).get("version"),
                            NVD(
                                CVSSv3(
                                    enrichments.get(vuln["id"], {}).get("vectorString", ""),
                                    enrichments.get(vuln["id"], {}).get("baseScore", ""),
                                )
                            ),
                        ),
                    )
                    for vuln in pkg_vulns
                ],
            )
        )

    return features
