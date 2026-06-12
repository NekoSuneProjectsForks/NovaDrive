"""Administrative destructive operations: purge a user's data, delete accounts.

These run as the acting admin and are intentionally thorough — they remove the
underlying stored objects/chunks (not just database rows) and clean up every
foreign-key dependent so a user row can be deleted without integrity errors.
"""

from __future__ import annotations

import logging

from novadrive.extensions import db
from novadrive.models import (
    ActivityLog,
    ExternalUpload,
    File,
    Folder,
    ObsOverlaySettings,
    RemoteDownload,
    ShareLink,
    SharedDrive,
    SharedDriveJoinRequest,
    SharedDriveMember,
    ShortUrl,
    User,
    UserSession,
)
from novadrive.services.activity_service import ActivityService
from novadrive.services.auth_service import AuthService
from novadrive.services.file_service import FileService

logger = logging.getLogger(__name__)


class AdminActionError(ValueError):
    """A destructive admin action was rejected (e.g. would remove the last admin)."""


class AdminService:
    @staticmethod
    def purge_user_data(target: User, actor: User, *, delete_root: bool = False) -> dict[str, int]:
        """Hard-delete a user's personal files and folders (storage included).

        Shared-drive content is left untouched. The user's root folder is kept
        unless ``delete_root`` is set (used by full account deletion).
        """
        counts = {"files": 0, "folders": 0}

        files = (
            File.query.filter_by(owner_id=target.id)
            .filter(File.shared_drive_id.is_(None))
            .all()
        )
        for file_record in files:
            ShareLink.query.filter_by(file_id=file_record.id).delete()
            FileService.delete_file(actor, file_record, hard_delete=True)
            counts["files"] += 1

        # Download jobs reference folders/files we are about to remove.
        RemoteDownload.query.filter_by(owner_id=target.id).delete()
        # Overlay settings may point at a folder being deleted.
        ObsOverlaySettings.query.filter_by(user_id=target.id).update(
            {ObsOverlaySettings.folder_id: None}, synchronize_session=False
        )

        folder_query = Folder.query.filter_by(owner_id=target.id).filter(
            Folder.shared_drive_id.is_(None)
        )
        if not delete_root:
            folder_query = folder_query.filter(Folder.is_root.is_(False))
        folder_ids = [folder.id for folder in folder_query.all()]
        if folder_ids:
            # Break the self-referential parent link before bulk deletion so the
            # rows can be removed in any order.
            Folder.query.filter(Folder.id.in_(folder_ids)).update(
                {Folder.parent_id: None}, synchronize_session=False
            )
            Folder.query.filter(Folder.id.in_(folder_ids)).delete(synchronize_session=False)
            counts["folders"] = len(folder_ids)

        db.session.commit()

        ActivityService.log(
            action="admin.user.data_purged",
            target_type="user",
            target_id=target.id,
            user_id=actor.id,
            metadata=counts,
        )
        logger.info(
            "Admin %s purged data for user %s: %s files, %s folders",
            actor.id,
            target.id,
            counts["files"],
            counts["folders"],
        )
        return counts

    @staticmethod
    def delete_account(target: User, actor: User) -> dict[str, int]:
        """Delete a user account and all of their data.

        Personal files/folders are hard-deleted. Shared-drive content and drives
        the user owned are reassigned to ``actor`` so other members keep access.
        """
        if target.id == actor.id:
            raise AdminActionError("You cannot delete your own account.")
        if target.role == "admin" and AuthService.count_admins() <= 1:
            raise AdminActionError("At least one admin account must remain.")

        target_id = target.id
        target_username = target.username

        counts = AdminService.purge_user_data(target, actor, delete_root=True)

        # Reassign any shared-drive content + drives owned by the target.
        File.query.filter_by(owner_id=target_id).update(
            {File.owner_id: actor.id}, synchronize_session=False
        )
        Folder.query.filter_by(owner_id=target_id).update(
            {Folder.owner_id: actor.id}, synchronize_session=False
        )
        SharedDrive.query.filter_by(owner_id=target_id).update(
            {SharedDrive.owner_id: actor.id}, synchronize_session=False
        )
        SharedDrive.query.filter_by(created_by_id=target_id).update(
            {SharedDrive.created_by_id: actor.id}, synchronize_session=False
        )

        # Memberships / join requests (NOT NULL user link → delete the rows).
        SharedDriveMember.query.filter_by(user_id=target_id).delete(synchronize_session=False)
        SharedDriveMember.query.filter_by(invited_by_id=target_id).update(
            {SharedDriveMember.invited_by_id: None}, synchronize_session=False
        )
        SharedDriveJoinRequest.query.filter_by(user_id=target_id).delete(synchronize_session=False)
        SharedDriveJoinRequest.query.filter_by(resolved_by_id=target_id).update(
            {SharedDriveJoinRequest.resolved_by_id: None}, synchronize_session=False
        )

        UserSession.query.filter_by(user_id=target_id).delete(synchronize_session=False)
        ObsOverlaySettings.query.filter_by(user_id=target_id).delete(synchronize_session=False)
        RemoteDownload.query.filter_by(owner_id=target_id).delete(synchronize_session=False)
        ExternalUpload.query.filter_by(owner_id=target_id).delete(synchronize_session=False)
        ShortUrl.query.filter_by(owner_id=target_id).delete(synchronize_session=False)

        # Preserve the audit trail but detach it from the deleted user.
        ActivityLog.query.filter_by(user_id=target_id).update(
            {ActivityLog.user_id: None}, synchronize_session=False
        )

        # Drop stale identity-map state from the bulk updates/deletes above so the
        # unit-of-work sees empty (already-reassigned) collections on delete.
        db.session.flush()
        db.session.expire_all()
        target = db.session.get(User, target_id)
        if target is not None:
            db.session.delete(target)
        db.session.commit()

        ActivityService.log(
            action="admin.user.deleted",
            target_type="user",
            target_id=target_id,
            user_id=actor.id,
            metadata={"username": target_username, **counts},
        )
        logger.info("Admin %s deleted account %s (%s)", actor.id, target_id, target_username)
        return counts

    @staticmethod
    def bulk(action: str, user_ids: list[int], actor: User) -> dict[str, object]:
        """Apply ``delete`` or ``purge`` to many users, skipping invalid targets."""
        if action not in {"delete", "purge"}:
            raise AdminActionError("Unknown bulk action.")

        processed = 0
        skipped: list[str] = []
        for user_id in user_ids:
            target = db.session.get(User, user_id)
            if target is None:
                continue
            try:
                if action == "delete":
                    AdminService.delete_account(target, actor)
                else:
                    AdminService.purge_user_data(target, actor)
                processed += 1
            except AdminActionError as exc:
                skipped.append(f"{target.username}: {exc}")
            except Exception:  # noqa: BLE001 - keep going through the batch
                db.session.rollback()
                logger.exception("Bulk %s failed for user %s", action, user_id)
                skipped.append(f"user #{user_id}: unexpected error")
        return {"processed": processed, "skipped": skipped}
