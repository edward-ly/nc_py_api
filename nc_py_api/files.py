"""
Nextcloud API for working with file system.
"""

from dataclasses import dataclass
from datetime import datetime
from email.utils import parsedate_to_datetime
from io import BytesIO
from json import dumps, loads
from os import path as p
from pathlib import Path
from typing import Optional, TypedDict, Union
from urllib.parse import unquote
from xml.etree import ElementTree

import xmltodict
from httpx import Response

from ._session import NcSessionBasic
from .exceptions import NextcloudException, check_error


class FsNodeInfo(TypedDict):
    nc_id: str
    fileid: int
    etag: str
    size: int
    content_length: int
    last_modified: datetime
    permissions: str
    favorite: bool


@dataclass
class FsNode:
    def __init__(self, user: str, path: str, name: str, **kwargs):
        self.user = user
        self.path = path
        self.name = name
        self.info: FsNodeInfo = {
            "nc_id": kwargs.get("nc_id", ""),
            "fileid": kwargs.get("fileid", 0),
            "etag": kwargs.get("etag", ""),
            "size": kwargs.get("size", 0),
            "content_length": kwargs.get("content_length", 0),
            "last_modified": datetime(1970, 1, 1),
            "permissions": kwargs.get("permissions", ""),
            "favorite": kwargs.get("favorite", False),
        }
        if "last_modified" in kwargs:
            self.last_modified = kwargs["last_modified"]

    @property
    def last_modified(self) -> datetime:
        return self.info["last_modified"]

    @last_modified.setter
    def last_modified(self, value: Union[str, datetime]):
        if isinstance(value, str):
            self.info["last_modified"] = parsedate_to_datetime(value)
        else:
            self.info["last_modified"] = value

    @property
    def is_dir(self) -> bool:
        return self.path.endswith("/")

    @property
    def full_path(self) -> str:
        return f"{self.user}/{self.path.lstrip('/')}" if self.user else self.path

    def __str__(self):
        return (
            f"{'Dir' if self.is_dir else 'File'}: `{self.name}` with id={self.info['fileid']}"
            f" last modified at {str(self.last_modified)} and {self.info['permissions']} permissions."
        )


PROPFIND_PROPERTIES = [
    "d:resourcetype",
    "d:getlastmodified",
    "d:getcontentlength",
    "d:getetag",
    "oc:size",
    "oc:id",
    "oc:fileid",
    "oc:downloadURL",
    "oc:dDC",
    "oc:permissions",
    "oc:checksums",
    "oc:share-types",
    "oc:favorite",
    "nc:is-encrypted",
    "nc:lock",
    "nc:lock-owner-displayname",
    "nc:lock-owner",
    "nc:lock-owner-type",
    "nc:lock-owner-editor",
    "nc:lock-time",
    "nc:lock-timeout",
]

SEARCH_PROPERTIES_MAP = {
    "name:": "d:displayname",  # like, eq
    "mime": "d:getcontenttype",  # like, eq
    "last_modified": "d:getlastmodified",  # gt, eq, lt
    "size": "oc:size",  # gt, gte, eq, lt
    "favorite": "oc:favorite",  # eq
    "fileid": "oc:fileid",  # eq
}


class FilesAPI:
    def __init__(self, session: NcSessionBasic):
        self._session = session

    def listdir(self, path="", exclude_self=True, root=False) -> list[FsNode]:
        properties = PROPFIND_PROPERTIES
        return self._listdir("" if root else self._session.user, path, properties=properties, exclude_self=exclude_self)

    def by_id(self, fileid: int) -> Optional[FsNode]:
        result = self.find(req=["eq", "fileid", fileid])
        return result[0] if result else None

    def by_path(self, path: str) -> Optional[FsNode]:
        result = self.listdir(path, exclude_self=False)
        return result[0] if result else None

    def find(self, req: list, path="", depth=-1) -> list[FsNode]:
        # `req` possible keys: "name", "mime", "last_modified", "size", "favorite", "fileid"
        root = ElementTree.Element(
            "d:searchrequest",
            attrib={"xmlns:d": "DAV:", "xmlns:oc": "http://owncloud.org/ns", "xmlns:nc": "http://nextcloud.org/ns"},
        )
        xml_search = ElementTree.SubElement(root, "d:basicsearch")
        xml_select_prop = ElementTree.SubElement(ElementTree.SubElement(xml_search, "d:select"), "d:prop")
        for i in PROPFIND_PROPERTIES:
            ElementTree.SubElement(xml_select_prop, i)
        xml_from_scope = ElementTree.SubElement(ElementTree.SubElement(xml_search, "d:from"), "d:scope")
        if path.startswith("/"):
            href = f"/files/{self._session.user}{path}"
        else:
            href = f"/files/{self._session.user}/{path}"
        ElementTree.SubElement(xml_from_scope, "d:href").text = href
        xml_from_scope_depth = ElementTree.SubElement(xml_from_scope, "d:depth")
        if depth == -1:
            xml_from_scope_depth.text = "infinity"
        else:
            xml_from_scope_depth.text = str(depth)
        xml_where = ElementTree.SubElement(xml_search, "d:where")
        self._build_search_req(xml_where, req)

        headers = {"Content-Type": "text/xml"}
        webdav_response = self._session.dav("SEARCH", "", data=self._element_tree_as_str(root), headers=headers)
        request_info = f"find: {self._session.user}, {req}, {path}, {depth}"
        return self._lf_parse_webdav_records(webdav_response, self._session.user, request_info)

    def download(self, path: str) -> bytes:
        """Downloads and returns the contents of a file.

        :param path: Path to a file to download relative to root directory of the user.
        """

        response = self._session.dav("GET", self._dav_get_obj_path(self._session.user, path))
        check_error(response.status_code, f"download: user={self._session.user}, path={path}")
        return response.content

    def download2stream(self, path: str, fp, **kwargs) -> None:
        """Downloads file to the given `fp` object.

        :param path: Path to a file to download relative to root directory of the user.
        :param fp: A filename (string), pathlib.Path object or a file object.
            The object must implement the ``file.write`` method and be able to write binary data.
        :param kwargs: **chunk_size** an int value specifying chunk size to write. Default = **512Kb**
        """

        with self._session.dav_stream(
            "GET", self._dav_get_obj_path(self._session.user, path)
        ) as response:  # type: ignore
            check_error(response.status_code, f"download: user={self._session.user}, path={path}")
            for data_chunk in response.iter_raw(chunk_size=kwargs.get("chunk_size", 512 * 1024)):
                fp.write(data_chunk)

    def upload(self, path: str, content: Union[bytes, str]) -> None:
        response = self._session.dav("PUT", self._dav_get_obj_path(self._session.user, path), data=content)
        check_error(response.status_code, f"upload: user={self._session.user}, path={path}, size={len(content)}")

    def mkdir(self, path: str) -> None:
        response = self._session.dav("MKCOL", self._dav_get_obj_path(self._session.user, path))
        check_error(response.status_code, f"mkdir: user={self._session.user}, path={path}")

    def makedirs(self, path: str, exist_ok=False) -> None:
        _path = ""
        for i in Path(path).parts:
            _path = p.join(_path, i)
            if not exist_ok:
                self.mkdir(_path)
            else:
                try:
                    self.mkdir(_path)
                except NextcloudException as e:
                    if e.status_code != 405:
                        raise e from None

    def delete(self, path: str, not_fail=False) -> None:
        response = self._session.dav("DELETE", self._dav_get_obj_path(self._session.user, path))
        if response.status_code == 404 and not_fail:
            return
        check_error(response.status_code, f"delete: user={self._session.user}, path={path}")

    def move(self, path_src: str, path_dest: str, overwrite=False) -> None:
        dest = self._session.cfg.dav_endpoint + self._dav_get_obj_path(self._session.user, path_dest)
        headers = {"Destination": dest, "Overwrite": "T" if overwrite else "F"}
        response = self._session.dav(
            "MOVE",
            self._dav_get_obj_path(self._session.user, path_src),
            headers=headers,
        )
        check_error(response.status_code, f"move: user={self._session.user}, src={path_src}, dest={dest}, {overwrite}")

    def copy(self, path_src: str, path_dest: str, overwrite=False) -> None:
        dest = self._session.cfg.dav_endpoint + self._dav_get_obj_path(self._session.user, path_dest)
        headers = {"Destination": dest, "Overwrite": "T" if overwrite else "F"}
        response = self._session.dav(
            "COPY",
            self._dav_get_obj_path(self._session.user, path_src),
            headers=headers,
        )
        check_error(response.status_code, f"copy: user={self._session.user}, src={path_src}, dest={dest}, {overwrite}")

    def listfav(self) -> list[FsNode]:
        root = ElementTree.Element(
            "oc:filter-files",
            attrib={"xmlns:d": "DAV:", "xmlns:oc": "http://owncloud.org/ns", "xmlns:nc": "http://nextcloud.org/ns"},
        )
        xml_filter_rules = ElementTree.SubElement(root, "oc:filter-rules")
        ElementTree.SubElement(xml_filter_rules, "oc:favorite").text = "1"
        webdav_response = self._session.dav(
            "REPORT", self._dav_get_obj_path(self._session.user), data=self._element_tree_as_str(root)
        )
        request_info = f"listfav: {self._session.user}"
        check_error(webdav_response.status_code, request_info)
        return self._lf_parse_webdav_records(webdav_response, self._session.user, request_info, favorite=True)

    def setfav(self, path: str, value: Union[int, bool]) -> None:
        root = ElementTree.Element(
            "d:propertyupdate",
            attrib={"xmlns:d": "DAV:", "xmlns:oc": "http://owncloud.org/ns"},
        )
        xml_set = ElementTree.SubElement(root, "d:set")
        xml_set_prop = ElementTree.SubElement(xml_set, "d:prop")
        ElementTree.SubElement(xml_set_prop, "oc:favorite").text = str(int(bool(value)))
        webdav_response = self._session.dav(
            "PROPPATCH", self._dav_get_obj_path(self._session.user, path), data=self._element_tree_as_str(root)
        )
        check_error(webdav_response.status_code, f"setfav: path={path}, value={value}")

    def _listdir(self, user: str, path: str, properties: list[str], exclude_self: bool) -> list[FsNode]:
        root = ElementTree.Element(
            "d:propfind",
            attrib={"xmlns:d": "DAV:", "xmlns:oc": "http://owncloud.org/ns", "xmlns:nc": "http://nextcloud.org/ns"},
        )
        prop = ElementTree.SubElement(root, "d:prop")
        for i in properties:
            ElementTree.SubElement(prop, i)
        webdav_response = self._session.dav(
            "PROPFIND", self._dav_get_obj_path(user, path), data=self._element_tree_as_str(root)
        )
        request_info = f"list: {user}, {path}, {properties}"
        result = self._lf_parse_webdav_records(webdav_response, user, request_info)
        if exclude_self:
            full_path = f"{user}/{path}".rstrip("/") if user else path.rstrip("/")
            for index, v in enumerate(result):
                if v.full_path.rstrip("/") == full_path:
                    del result[index]
                    break
        return result

    def _parse_records(self, fs_records: list[dict], user: str, favorite: bool):
        result: list[FsNode] = []
        for record in fs_records:
            obj_full_path = unquote(record.get("d:href", ""))
            obj_name = obj_full_path.rstrip("/").rsplit("/", maxsplit=1)[-1]
            if not obj_name:
                continue
            dav_full_path = self._session.cfg.dav_url_suffix + self._dav_get_obj_path(user)
            obg_rel_path = obj_full_path.replace(dav_full_path, "").lstrip("/")
            propstat = record["d:propstat"]
            fs_node = self._parse_record(
                propstat if isinstance(propstat, list) else [propstat], user, obg_rel_path, obj_name
            )
            if favorite and not fs_node.info["fileid"]:
                _fs_node = self.by_path(fs_node.path)
                if _fs_node:
                    _fs_node.info["favorite"] = True
                    result.append(_fs_node)
            elif fs_node.info["fileid"]:
                result.append(fs_node)
        return result

    @staticmethod
    def _parse_record(prop_stats: list[dict], user: str, obg_rel_path: str, obj_name: str) -> FsNode:
        fs_node = FsNode(user=user, path=obg_rel_path, name=obj_name)
        for prop_stat in prop_stats:
            if str(prop_stat.get("d:status", "")).find("200 OK") == -1:
                continue
            prop: dict = prop_stat["d:prop"]
            prop_keys = prop.keys()
            if "oc:id" in prop_keys:
                fs_node.info["nc_id"] = prop["oc:id"]
            if "oc:fileid" in prop_keys:
                fs_node.info["fileid"] = int(prop["oc:fileid"])
            if "oc:size" in prop_keys:
                fs_node.info["size"] = int(prop["oc:size"])
            if "d:getcontentlength" in prop_keys:
                fs_node.info["content_length"] = int(prop["d:getcontentlength"])
            if "d:getetag" in prop_keys:
                fs_node.info["etag"] = prop["d:getetag"]
            if "d:getlastmodified" in prop_keys:
                try:
                    fs_node.last_modified = prop["d:getlastmodified"]
                except ValueError:
                    pass
            if "oc:permissions" in prop_keys:
                fs_node.info["permissions"] = prop["oc:permissions"]
            if "oc:favorite" in prop_keys:
                fs_node.info["favorite"] = bool(int(prop["oc:favorite"]))
            # xz = prop.get("oc:dDC", "")
        return fs_node

    def _lf_parse_webdav_records(self, webdav_res: Response, user: str, info: str, favorite=False) -> list[FsNode]:
        check_error(webdav_res.status_code, info=info)
        if webdav_res.status_code != 207:  # multistatus
            raise NextcloudException(webdav_res.status_code, "Response is not a multistatus.", info=info)
        if not webdav_res.text:
            raise NextcloudException(webdav_res.status_code, "Response is empty.", info=info)
        response_data = loads(dumps(xmltodict.parse(webdav_res.text)))
        if "d:error" in response_data:
            err = response_data["d:error"]
            raise NextcloudException(reason=f'{err["s:exception"]}: {err["s:message"]}'.replace("\n", ""), info=info)
        response = response_data["d:multistatus"].get("d:response", [])
        return self._parse_records([response] if isinstance(response, dict) else response, user, favorite)

    @staticmethod
    def _dav_get_obj_path(user: str, path: str = "") -> str:
        obj_dav_path = "/files"
        if user:
            obj_dav_path += "/" + user
        if path:
            obj_dav_path += "/" + path.lstrip("/")
        return obj_dav_path

    @staticmethod
    def _element_tree_as_str(element) -> str:
        with BytesIO() as buffer:
            ElementTree.ElementTree(element).write(buffer, xml_declaration=True)
            buffer.seek(0)
            return buffer.read().decode("utf-8")

    @staticmethod
    def _build_search_req(xml_element_where, req: list) -> None:
        def _process_or_and(xml_element, or_and: str):
            _where_part_root = ElementTree.SubElement(xml_element, f"d:{or_and}")
            _add_value(_where_part_root)
            _add_value(_where_part_root)

        def _add_value(xml_element, val=None) -> None:
            first_val = req.pop(0) if val is None else val
            if first_val in ("or", "and"):
                _process_or_and(xml_element, first_val)
                return
            _root = ElementTree.SubElement(xml_element, f"d:{first_val}")
            _ = ElementTree.SubElement(_root, "d:prop")
            ElementTree.SubElement(_, SEARCH_PROPERTIES_MAP[req.pop(0)])
            _ = ElementTree.SubElement(_root, "d:literal")
            value = req.pop(0)
            _.text = value if isinstance(value, str) else str(value)

        while len(req):
            where_part = req.pop(0)
            if where_part in ("or", "and"):
                _process_or_and(xml_element_where, where_part)
            else:
                _add_value(xml_element_where, where_part)
