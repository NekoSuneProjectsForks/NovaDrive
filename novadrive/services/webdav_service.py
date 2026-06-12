from __future__ import annotations

import mimetypes
import uuid
from dataclasses import dataclass
from email.utils import format_datetime
from urllib.parse import quote, unquote, urlparse
from xml.etree.ElementTree import Element, SubElement, tostring

from flask import current_app, request
from werkzeug.datastructures import FileStorage

from novadrive.models import File, Folder, User
from novadrive.services.auth_service import AuthService
from novadrive.services.file_delivery import FileDeliveryService
from novadrive.services.file_service import FileService


class WebDavError(ValueError):
    def __init__(self, message: str, status_code: int):
        super().__init__(message)
        self.status_code = status_code


@dataclass
class DavResource:
    relative_path: str
    folder: Folder | None = None
    file: File | None = None

    @property
    def exists(self) -> bool:
        return self.folder is not None or self.file is not None

    @property
    def is_collection(self) -> bool:
        return self.folder is not None

    @property
    def name(self) -> str:
        if self.folder is not None:
            return self.folder.name
        if self.file is not None:
            return self.file.filename
        return ""


class WebDavService:
    @staticmethod
    def authenticate_request() -> User | None:
        auth = request.authorization
        if not auth or not auth.username or auth.password is None:
            return None

        user = AuthService.authenticate_webdav_password(auth.username, auth.password)
        if not user:
            return None
        if not AuthService.can_use_password_login(user, current_app.config):
            return None
        if AuthService.must_change_default_admin_credentials(user):
            return None
        return user

    @staticmethod
    def resolve_resource(user: User, resource_path: str) -> DavResource:
        normalized_path = WebDavService.normalize_path(resource_path)
        root = AuthService.get_root_folder(user)
        if not normalized_path:
            return DavResource(relative_path="", folder=root)

        segments = normalized_path.split("/")
        current = root
        traversed: list[str] = []

        for segment in segments[:-1]:
            next_folder = (
                Folder.query.filter_by(
                    parent_id=current.id,
                    owner_id=user.id,
                    deleted_at=None,
                    name=segment,
                )
                .order_by(Folder.id.asc())
                .first()
            )
            if not next_folder:
                return DavResource(relative_path="/".join(segments))
            current = next_folder
            traversed.append(segment)

        leaf = segments[-1]
        child_folder = (
            Folder.query.filter_by(
                parent_id=current.id,
                owner_id=user.id,
                deleted_at=None,
                name=leaf,
            )
            .order_by(Folder.id.asc())
            .first()
        )
        child_file = (
            File.query.filter_by(
                folder_id=current.id,
                owner_id=user.id,
                deleted_at=None,
                upload_status="complete",
                filename=leaf,
            )
            .order_by(File.id.asc())
            .first()
        )
        return DavResource(relative_path="/".join(segments), folder=child_folder, file=child_file)

    @staticmethod
    def resolve_parent_folder(user: User, resource_path: str) -> tuple[Folder, str]:
        normalized_path = WebDavService.normalize_path(resource_path)
        if not normalized_path:
            raise WebDavError("The WebDAV root cannot be modified directly.", 403)

        segments = normalized_path.split("/")
        leaf = segments[-1]
        parent_path = "/".join(segments[:-1])
        parent_resource = WebDavService.resolve_resource(user, parent_path)
        if not parent_resource.folder:
            raise WebDavError("Parent folder not found.", 409)
        return parent_resource.folder, leaf

    @staticmethod
    def list_folder_children(user: User, folder: Folder) -> list[DavResource]:
        child_folders = (
            Folder.query.filter_by(parent_id=folder.id, owner_id=user.id, deleted_at=None)
            .order_by(Folder.name.asc())
            .all()
        )
        child_files = (
            File.query.filter_by(folder_id=folder.id, owner_id=user.id, deleted_at=None, upload_status="complete")
            .order_by(File.filename.asc())
            .all()
        )

        resources = [DavResource(relative_path=WebDavService.relative_path_for_folder(user, child), folder=child) for child in child_folders]
        resources.extend(
            DavResource(relative_path=WebDavService.relative_path_for_file(user, child), file=child)
            for child in child_files
        )
        return resources

    @staticmethod
    def put_file(user: User, resource_path: str) -> int:
        parent_folder, filename = WebDavService.resolve_parent_folder(user, resource_path)
        existing = WebDavService.resolve_resource(user, resource_path)
        overwrite = request.headers.get("Overwrite", "T").upper() != "F"

        if existing.folder:
            raise WebDavError("A folder already exists at that path.", 405)
        if existing.file and not overwrite:
            raise WebDavError("Destination exists and overwrite is disabled.", 412)

        folder_conflict = (
            Folder.query.filter_by(
                parent_id=parent_folder.id,
                owner_id=user.id,
                deleted_at=None,
                name=filename,
            )
            .first()
        )
        if folder_conflict:
            raise WebDavError("A folder already uses that path.", 409)

        if existing.file:
            FileService.delete_file(user, existing.file, hard_delete=True)

        guessed_type = request.content_type or mimetypes.guess_type(filename)[0] or "application/octet-stream"
        upload = FileStorage(
            stream=request.stream,
            filename=filename,
            name="file",
            content_type=guessed_type,
        )
        record = FileService.upload_single_file(user, parent_folder, upload, current_app.config)
        if record.filename != filename:
            raise WebDavError("Could not preserve the requested file name.", 409)
        return 204 if existing.file else 201

    @staticmethod
    def make_collection(user: User, resource_path: str) -> None:
        parent_folder, name = WebDavService.resolve_parent_folder(user, resource_path)
        existing = WebDavService.resolve_resource(user, resource_path)
        if existing.exists:
            raise WebDavError("That path already exists.", 405)

        file_conflict = (
            File.query.filter_by(
                folder_id=parent_folder.id,
                owner_id=user.id,
                deleted_at=None,
                upload_status="complete",
                filename=name,
            )
            .first()
        )
        if file_conflict:
            raise WebDavError("A file already uses that path.", 409)

        FileService.create_folder(user, parent_folder, name)

    @staticmethod
    def delete_resource(user: User, resource_path: str) -> None:
        resource = WebDavService.resolve_resource(user, resource_path)
        if resource.file:
            FileService.delete_file(user, resource.file, hard_delete=True)
            return
        if resource.folder:
            FileService.delete_folder(user, resource.folder, hard_delete=True)
            return
        raise WebDavError("Resource not found.", 404)

    @staticmethod
    def move_resource(user: User, source_path: str) -> None:
        source = WebDavService.resolve_resource(user, source_path)
        if not source.exists:
            raise WebDavError("Source not found.", 404)
        if source.folder and source.folder.is_root:
            raise WebDavError("The WebDAV root cannot be moved.", 403)

        destination_path = WebDavService.destination_relative_path()
        destination_parent, destination_name = WebDavService.resolve_parent_folder(user, destination_path)
        destination = WebDavService.resolve_resource(user, destination_path)
        overwrite = request.headers.get("Overwrite", "T").upper() != "F"

        if source.file and destination.file and source.file.id == destination.file.id:
            return
        if source.folder and destination.folder and source.folder.id == destination.folder.id:
            return

        if destination.exists and not overwrite:
            raise WebDavError("Destination exists and overwrite is disabled.", 412)

        folder_conflict = (
            Folder.query.filter_by(
                parent_id=destination_parent.id,
                owner_id=user.id,
                deleted_at=None,
                name=destination_name,
            )
            .first()
        )
        file_conflict = (
            File.query.filter_by(
                folder_id=destination_parent.id,
                owner_id=user.id,
                deleted_at=None,
                upload_status="complete",
                filename=destination_name,
            )
            .first()
        )

        if source.file:
            if destination.folder or (folder_conflict and (not destination.folder or folder_conflict.id != destination.folder.id)):
                raise WebDavError("Destination path is blocked by a folder.", 409)
            if destination.file and destination.file.id != source.file.id:
                FileService.delete_file(user, destination.file, hard_delete=True)
            if source.file.folder_id != destination_parent.id:
                FileService.move_file(user, source.file, destination_parent)
            if source.file.filename != destination_name:
                FileService.rename_file(user, source.file, destination_name)
            return

        if destination.exists:
            raise WebDavError("Replacing folders through WebDAV move is not supported.", 409)
        if file_conflict:
            raise WebDavError("Destination path is blocked by a file.", 409)
        if source.folder.owner_id != destination_parent.owner_id:
            raise WebDavError("Folder ownership mismatch.", 403)
        if source.folder.id != destination_parent.id:
            FileService.move_folder(user, source.folder, destination_parent)
        if source.folder.name != destination_name:
            FileService.rename_folder(user, source.folder, destination_name)

    @staticmethod
    def build_propfind_response(user: User, resource_path: str, depth_header: str | None) -> bytes:
        resource = WebDavService.resolve_resource(user, resource_path)
        if not resource.exists:
            raise WebDavError("Resource not found.", 404)

        multistatus = Element("{DAV:}multistatus")
        resources = [resource]

        depth = (depth_header or "0").strip().lower()
        if depth in {"1", "infinity"} and resource.folder:
            resources.extend(WebDavService.list_folder_children(user, resource.folder))

        for item in resources:
            response = SubElement(multistatus, "{DAV:}response")
            href = SubElement(response, "{DAV:}href")
            href.text = WebDavService.absolute_href(item.relative_path, is_collection=item.is_collection)

            propstat = SubElement(response, "{DAV:}propstat")
            prop = SubElement(propstat, "{DAV:}prop")
            displayname = SubElement(prop, "{DAV:}displayname")
            displayname.text = item.folder.name if item.folder else item.file.filename

            creationdate = SubElement(prop, "{DAV:}creationdate")
            lastmodified = SubElement(prop, "{DAV:}getlastmodified")
            resourcetype = SubElement(prop, "{DAV:}resourcetype")

            if item.folder:
                creationdate.text = item.folder.created_at.isoformat()
                lastmodified.text = format_datetime(item.folder.updated_at)
                SubElement(resourcetype, "{DAV:}collection")
            else:
                creationdate.text = item.file.created_at.isoformat()
                lastmodified.text = format_datetime(item.file.updated_at)
                SubElement(prop, "{DAV:}getcontentlength").text = str(item.file.total_size)
                SubElement(prop, "{DAV:}getcontenttype").text = item.file.mime_type

            status = SubElement(propstat, "{DAV:}status")
            status.text = "HTTP/1.1 200 OK"

        return tostring(multistatus, encoding="utf-8", xml_declaration=True)

    @staticmethod
    def raw_file_response(user: User, resource_path: str):
        resource = WebDavService.resolve_resource(user, resource_path)
        if not resource.file:
            raise WebDavError("File not found.", 404)
        return FileDeliveryService.build_response(
            resource.file,
            current_app.config,
            as_attachment=False,
            download_name=resource.file.filename,
        )

    @staticmethod
    def normalize_path(resource_path: str | None) -> str:
        raw = (resource_path or "").strip("/")
        if not raw:
            return ""

        segments = [segment for segment in raw.split("/") if segment]
        if any(segment in {".", ".."} for segment in segments):
            raise WebDavError("Invalid path.", 400)
        return "/".join(segments)

    @staticmethod
    def destination_relative_path() -> str:
        destination_header = request.headers.get("Destination", "")
        if not destination_header:
            raise WebDavError("Missing Destination header.", 400)

        parsed = urlparse(destination_header)
        destination_path = unquote(parsed.path or destination_header)
        dav_root = f"{request.script_root.rstrip('/')}/dav"
        if not destination_path.startswith(dav_root):
            raise WebDavError("Destination must stay inside the NovaDrive WebDAV root.", 400)
        return WebDavService.normalize_path(destination_path[len(dav_root) :])

    @staticmethod
    def relative_path_for_folder(user: User, folder: Folder) -> str:
        root = AuthService.get_root_folder(user)
        segments: list[str] = []
        current = folder
        while current is not None and current.id != root.id:
            segments.append(current.name)
            current = current.parent
        return "/".join(reversed(segments))

    @staticmethod
    def relative_path_for_file(user: User, file_record: File) -> str:
        folder_path = WebDavService.relative_path_for_folder(user, file_record.folder)
        return "/".join([part for part in [folder_path, file_record.filename] if part])

    @staticmethod
    def build_proppatch_response(user: User, resource_path: str) -> bytes:
        # NovaDrive does not persist arbitrary dead properties (e.g. the Win32
        # timestamps Windows tries to set), but acknowledging them keeps the
        # redirector happy so writes complete. Report the resource as updated.
        resource = WebDavService.resolve_resource(user, resource_path)
        normalized = WebDavService.normalize_path(resource_path)
        multistatus = Element("{DAV:}multistatus")
        response = SubElement(multistatus, "{DAV:}response")
        href = SubElement(response, "{DAV:}href")
        href.text = WebDavService.absolute_href(normalized, is_collection=resource.is_collection)
        propstat = SubElement(response, "{DAV:}propstat")
        SubElement(propstat, "{DAV:}prop")
        status = SubElement(propstat, "{DAV:}status")
        status.text = "HTTP/1.1 200 OK"
        return tostring(multistatus, encoding="utf-8", xml_declaration=True)

    @staticmethod
    def build_lock_response(resource_path: str) -> tuple[bytes, str]:
        # Synthetic lock: NovaDrive does not enforce WebDAV locks, but advertising
        # class-2 support and returning a token lets Windows/Office proceed with a
        # PUT. The path may not exist yet (clients LOCK before creating a file),
        # so the token is fabricated rather than resolved against storage.
        lock_token = f"opaquelocktoken:{uuid.uuid4()}"
        normalized = WebDavService.normalize_path(resource_path)

        prop = Element("{DAV:}prop")
        lockdiscovery = SubElement(prop, "{DAV:}lockdiscovery")
        activelock = SubElement(lockdiscovery, "{DAV:}activelock")
        SubElement(SubElement(activelock, "{DAV:}locktype"), "{DAV:}write")
        SubElement(SubElement(activelock, "{DAV:}lockscope"), "{DAV:}exclusive")
        SubElement(activelock, "{DAV:}depth").text = "infinity"
        SubElement(activelock, "{DAV:}timeout").text = "Second-3600"
        locktoken_el = SubElement(activelock, "{DAV:}locktoken")
        SubElement(locktoken_el, "{DAV:}href").text = lock_token
        lockroot = SubElement(activelock, "{DAV:}lockroot")
        SubElement(lockroot, "{DAV:}href").text = WebDavService.absolute_href(
            normalized, is_collection=False
        )
        return tostring(prop, encoding="utf-8", xml_declaration=True), lock_token

    @staticmethod
    def absolute_href(relative_path: str, *, is_collection: bool) -> str:
        dav_base = f"{request.script_root.rstrip('/')}/dav"
        segments = [quote(segment) for segment in relative_path.split("/") if segment]
        path = dav_base
        if segments:
            path = f"{path}/{'/'.join(segments)}"
        if is_collection and not path.endswith("/"):
            path = f"{path}/"
        return path or f"{dav_base}/"
