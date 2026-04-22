from __future__ import annotations

from novadrive.extensions import db
from novadrive.models import (
    Folder,
    SharedDrive,
    SharedDriveJoinRequest,
    SharedDriveMember,
    User,
    utcnow,
)
from novadrive.services.activity_service import ActivityService


class SharedDriveService:
    VISIBILITIES = {"invite_only", "request_access", "public"}
    MEMBER_ROLES = {"owner", "editor", "viewer"}

    @staticmethod
    def normalize_visibility(value: str | None) -> str:
        normalized = (value or "invite_only").strip().lower()
        if normalized not in SharedDriveService.VISIBILITIES:
            raise ValueError("Invalid shared drive visibility.")
        return normalized

    @staticmethod
    def normalize_member_role(value: str | None) -> str:
        normalized = (value or "viewer").strip().lower()
        if normalized not in SharedDriveService.MEMBER_ROLES:
            raise ValueError("Invalid shared drive member role.")
        return normalized

    @staticmethod
    def get_drive(drive_id: int) -> SharedDrive | None:
        drive = db.session.get(SharedDrive, drive_id)
        if not drive or not drive.is_active:
            return None
        return drive

    @staticmethod
    def membership_for_user(shared_drive: SharedDrive, user: User | None) -> SharedDriveMember | None:
        if user is None:
            return None
        return SharedDriveMember.query.filter_by(
            shared_drive_id=shared_drive.id,
            user_id=user.id,
        ).first()

    @staticmethod
    def pending_request_for_user(shared_drive: SharedDrive, user: User | None) -> SharedDriveJoinRequest | None:
        if user is None:
            return None
        return SharedDriveJoinRequest.query.filter_by(
            shared_drive_id=shared_drive.id,
            user_id=user.id,
            status="pending",
        ).first()

    @staticmethod
    def can_view(shared_drive: SharedDrive, user: User | None) -> bool:
        if user is None:
            return False
        if user.is_admin:
            return True
        if SharedDriveService.membership_for_user(shared_drive, user):
            return True
        return shared_drive.visibility == "public"

    @staticmethod
    def can_write(shared_drive: SharedDrive, user: User | None) -> bool:
        if user is None:
            return False
        if user.is_admin:
            return True
        membership = SharedDriveService.membership_for_user(shared_drive, user)
        return bool(membership and membership.can_write)

    @staticmethod
    def can_manage(shared_drive: SharedDrive, user: User | None) -> bool:
        if user is None:
            return False
        if user.is_admin:
            return True
        membership = SharedDriveService.membership_for_user(shared_drive, user)
        return bool(membership and membership.can_manage)

    @staticmethod
    def list_member_drives(user: User) -> list[SharedDrive]:
        if user.is_admin:
            return SharedDrive.query.filter_by(is_active=True).order_by(SharedDrive.name.asc()).all()

        return (
            SharedDrive.query.join(
                SharedDriveMember,
                SharedDriveMember.shared_drive_id == SharedDrive.id,
            )
            .filter(
                SharedDrive.is_active.is_(True),
                SharedDriveMember.user_id == user.id,
            )
            .order_by(SharedDrive.name.asc())
            .all()
        )

    @staticmethod
    def list_discoverable_drives(user: User) -> list[SharedDrive]:
        if user.is_admin:
            return []

        member_drive_ids = (
            db.session.query(SharedDriveMember.shared_drive_id)
            .filter(SharedDriveMember.user_id == user.id)
            .subquery()
        )
        return (
            SharedDrive.query.filter(
                SharedDrive.is_active.is_(True),
                SharedDrive.visibility.in_(["public", "request_access"]),
                ~SharedDrive.id.in_(member_drive_ids),
            )
            .order_by(SharedDrive.name.asc())
            .all()
        )

    @staticmethod
    def visible_drives(user: User) -> list[SharedDrive]:
        drives = {drive.id: drive for drive in SharedDriveService.list_member_drives(user)}
        for drive in SharedDriveService.list_discoverable_drives(user):
            drives.setdefault(drive.id, drive)
        return sorted(drives.values(), key=lambda drive: drive.name.lower())

    @staticmethod
    def get_root_folder(shared_drive: SharedDrive) -> Folder:
        root = Folder.query.filter_by(
            shared_drive_id=shared_drive.id,
            is_root=True,
            deleted_at=None,
        ).first()
        if root:
            return root

        root = Folder(
            name=shared_drive.name,
            owner_id=shared_drive.owner_id,
            shared_drive_id=shared_drive.id,
            is_root=True,
        )
        db.session.add(root)
        db.session.commit()
        return root

    @staticmethod
    def create_shared_drive(
        *,
        name: str,
        owner: User,
        actor: User,
        description: str | None = None,
        storage_quota_bytes: int = 0,
        visibility: str = "invite_only",
    ) -> SharedDrive:
        normalized_name = (name or "").strip()
        if not normalized_name:
            raise ValueError("Shared drive name is required.")
        if storage_quota_bytes < 0:
            raise ValueError("Shared drive storage quota must be zero or greater.")

        shared_drive = SharedDrive(
            name=normalized_name,
            description=(description or "").strip() or None,
            owner_id=owner.id,
            created_by_id=actor.id,
            visibility=SharedDriveService.normalize_visibility(visibility),
            storage_quota_bytes=int(storage_quota_bytes),
        )
        db.session.add(shared_drive)
        db.session.flush()

        root_folder = Folder(
            name=normalized_name,
            owner_id=owner.id,
            shared_drive_id=shared_drive.id,
            is_root=True,
        )
        db.session.add(root_folder)
        db.session.flush()

        membership = SharedDriveMember(
            shared_drive_id=shared_drive.id,
            user_id=owner.id,
            role="owner",
            invited_by_id=actor.id,
        )
        db.session.add(membership)
        db.session.commit()

        ActivityService.log(
            action="shared_drive.created",
            target_type="shared_drive",
            target_id=shared_drive.id,
            user_id=actor.id,
            metadata={
                "owner_id": owner.id,
                "visibility": shared_drive.visibility,
                "storage_quota_bytes": int(storage_quota_bytes),
            },
        )
        return shared_drive

    @staticmethod
    def update_shared_drive(
        shared_drive: SharedDrive,
        *,
        actor: User,
        name: str | None = None,
        description: str | None = None,
        visibility: str | None = None,
        storage_quota_bytes: int | None = None,
    ) -> SharedDrive:
        updates: dict[str, object] = {}

        if name is not None:
            normalized_name = name.strip()
            if not normalized_name:
                raise ValueError("Shared drive name is required.")
            if shared_drive.name != normalized_name:
                shared_drive.name = normalized_name
                updates["name"] = normalized_name
                root_folder = SharedDriveService.get_root_folder(shared_drive)
                root_folder.name = normalized_name

        if description is not None:
            normalized_description = description.strip() or None
            if shared_drive.description != normalized_description:
                shared_drive.description = normalized_description
                updates["description"] = normalized_description or ""

        if visibility is not None:
            normalized_visibility = SharedDriveService.normalize_visibility(visibility)
            if shared_drive.visibility != normalized_visibility:
                shared_drive.visibility = normalized_visibility
                updates["visibility"] = normalized_visibility

        if storage_quota_bytes is not None:
            if storage_quota_bytes < 0:
                raise ValueError("Shared drive storage quota must be zero or greater.")
            normalized_quota = int(storage_quota_bytes)
            if int(shared_drive.storage_quota_bytes or 0) != normalized_quota:
                shared_drive.storage_quota_bytes = normalized_quota
                updates["storage_quota_bytes"] = normalized_quota

        db.session.commit()

        if updates:
            ActivityService.log(
                action="shared_drive.updated",
                target_type="shared_drive",
                target_id=shared_drive.id,
                user_id=actor.id,
                metadata=updates,
            )
        return shared_drive

    @staticmethod
    def add_member_by_email(
        shared_drive: SharedDrive,
        *,
        email: str,
        role: str,
        actor: User,
    ) -> SharedDriveMember:
        normalized_email = (email or "").strip().lower()
        if not normalized_email:
            raise ValueError("A user email is required.")

        user = User.query.filter(db.func.lower(User.email) == normalized_email).first()
        if not user:
            raise ValueError("No user with that email exists yet.")

        normalized_role = SharedDriveService.normalize_member_role(role)
        membership = SharedDriveService.membership_for_user(shared_drive, user)
        if membership:
            membership.role = "owner" if membership.user_id == shared_drive.owner_id else normalized_role
            membership.invited_by_id = actor.id
        else:
            membership = SharedDriveMember(
                shared_drive_id=shared_drive.id,
                user_id=user.id,
                role="owner" if user.id == shared_drive.owner_id else normalized_role,
                invited_by_id=actor.id,
            )
            db.session.add(membership)

        pending_request = SharedDriveService.pending_request_for_user(shared_drive, user)
        if pending_request:
            pending_request.status = "approved"
            pending_request.resolved_at = utcnow()
            pending_request.resolved_by_id = actor.id

        db.session.commit()
        ActivityService.log(
            action="shared_drive.member.added",
            target_type="shared_drive",
            target_id=shared_drive.id,
            user_id=actor.id,
            metadata={"member_user_id": user.id, "role": membership.role},
        )
        return membership

    @staticmethod
    def remove_member(shared_drive: SharedDrive, membership: SharedDriveMember, *, actor: User) -> None:
        if membership.role == "owner" or membership.user_id == shared_drive.owner_id:
            raise ValueError("The shared drive owner cannot be removed here.")

        db.session.delete(membership)
        db.session.commit()
        ActivityService.log(
            action="shared_drive.member.removed",
            target_type="shared_drive",
            target_id=shared_drive.id,
            user_id=actor.id,
            metadata={"member_user_id": membership.user_id},
        )

    @staticmethod
    def create_join_request(shared_drive: SharedDrive, *, user: User) -> SharedDriveJoinRequest:
        if shared_drive.visibility != "request_access":
            raise ValueError("This shared drive is not accepting join requests.")
        if SharedDriveService.membership_for_user(shared_drive, user):
            raise ValueError("You already have access to this shared drive.")

        pending = SharedDriveService.pending_request_for_user(shared_drive, user)
        if pending:
            raise ValueError("You already have a pending join request for this shared drive.")

        join_request = SharedDriveJoinRequest(
            shared_drive_id=shared_drive.id,
            user_id=user.id,
            status="pending",
        )
        db.session.add(join_request)
        db.session.commit()
        ActivityService.log(
            action="shared_drive.join_request.created",
            target_type="shared_drive",
            target_id=shared_drive.id,
            user_id=user.id,
        )
        return join_request

    @staticmethod
    def resolve_join_request(
        join_request: SharedDriveJoinRequest,
        *,
        actor: User,
        approve: bool,
    ) -> SharedDriveJoinRequest:
        if join_request.status != "pending":
            raise ValueError("That join request has already been processed.")

        if approve:
            existing_membership = SharedDriveService.membership_for_user(join_request.shared_drive, join_request.user)
            if not existing_membership:
                db.session.add(
                    SharedDriveMember(
                        shared_drive_id=join_request.shared_drive_id,
                        user_id=join_request.user_id,
                        role="viewer",
                        invited_by_id=actor.id,
                    )
                )
            join_request.status = "approved"
            action = "shared_drive.join_request.approved"
        else:
            join_request.status = "denied"
            action = "shared_drive.join_request.denied"

        join_request.resolved_at = utcnow()
        join_request.resolved_by_id = actor.id
        db.session.commit()
        ActivityService.log(
            action=action,
            target_type="shared_drive",
            target_id=join_request.shared_drive_id,
            user_id=actor.id,
            metadata={"request_user_id": join_request.user_id},
        )
        return join_request
