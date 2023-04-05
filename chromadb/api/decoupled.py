import json
import time
import re
from typing import Dict, List, Optional, Sequence, Callable, Type, cast
import chromadb.config
from chromadb.api import API
from chromadb.api.models.Collection import Collection
from chromadb.types import InsertEmbeddingRecord, InsertType
import chromadb.types
import chromadb.ingest
import chromadb.db
from chromadb.segment import SegmentManager, MetadataReader, VectorReader
import itertools
from chromadb.api.types import (
    Documents,
    Embedding,
    Embeddings,
    GetResult,
    IDs,
    Include,
    Metadatas,
    QueryResult,
    Where,
    WhereDocument,
    Union,
)
from chromadb.api.models.Collection import Collection
import uuid


class DecoupledAPI(API):
    """API that uses the new segment-based architecture in which reads and writes are decoupled."""

    ingest_impl: chromadb.ingest.Producer
    sysdb: chromadb.db.SysDB
    segment_manager: SegmentManager

    def __init__(self, settings):
        self.settings = settings
        self.ingest_impl = chromadb.config.get_component(settings, "chroma_ingest_impl")
        self.sysdb = chromadb.config.get_component(settings, "chroma_system_db_impl")
        self.segment_manager = chromadb.config.get_component(settings, "chroma_segment_manager")
        pass

    def heartbeat(self):
        return int(1000 * time.time_ns())

    def _collection_model(
        self, collection: chromadb.types.Collection, embedding_function: Optional[Callable]
    ) -> Collection:
        """Create a user-facing Collection model from a back-end Collection"""
        return Collection(
            client=self,
            name=collection["name"],
            id=collection["id"],
            metadata=collection["metadata"],
            topic=collection["topic"],
        )

    def _collection_id(self, collection_name: str):
        """Get the ID of a collection by name. Involves a round trip to the database, operations on collections
        should prefer using IDs whenever possible."""

        colls = self.sysdb.get_collections(name=collection_name)
        if len(colls) == 0:
            raise ValueError(f"Collection {collection_name} does not exist")
        return colls[0]["id"]

    #
    # COLLECTION METHODS
    #
    def list_collections(self) -> Sequence[Collection]:

        topics = self.sysdb.get_topics()
        collections = []
        for topic in topics:
            collections.append(self._collection(topic))

        return collections

    def create_collection(
        self,
        name: str,
        metadata: Optional[Dict[str, Union[str, int, float]]] = {},
        get_or_create: bool = False,
        embedding_function: Optional[Callable] = None,
    ) -> Collection:

        colls = self.sysdb.get_collections(name=name)

        if len(colls) > 0:
            if get_or_create:
                return self._collection_model(colls[0], embedding_function)
            else:
                raise ValueError(f"Collection {name} already exists")

        id = uuid.uuid4()
        # This will need to be revisted when we support multiple tenants
        # Or if we multiplex across topics
        topic = f"persistent://default/default/{id}"
        coll = chromadb.types.Collection(name=name, id=id, metadata=metadata, topic=topic)

        self.ingest_impl.create_topic(topic)
        self.sysdb.create_collection(coll)
        self.segment_manager.create_collection(coll)

        return self._collection_model(coll, embedding_function)

    def delete_collection(
        self,
        name: str,
    ):
        self.ingest_impl.delete_topic(name)
        self.sysdb.delete_collection(name)
        self.segment_manager.delete_topic_segments(name)

    def get_or_create_collection(
        self,
        name: str,
        metadata: Optional[Dict] = None,
        embedding_function: Optional[Callable] = None,
    ) -> Collection:
        """Calls create_collection with get_or_create=True

        Args:
            name (str): The name of the collection to create. The name must be unique.
            metadata (Optional[Dict], optional): A dictionary of metadata to associate with the collection. Defaults to None.
        Returns:
            dict: the created collection

        """
        return self.create_collection(
            name, metadata, embedding_function=embedding_function, get_or_create=True
        )

    def get_collection(
        self,
        name: str,
        embedding_function: Optional[Callable] = None,
    ) -> Collection:

        colls = self.sysdb.get_collections(name=name)
        if len(colls) == 0:
            raise ValueError(f"Collection {name} does not exist")
        return self._collection_model(colls[0], embedding_function)

    def _modify(
        self,
        current_name: str,
        new_name: Optional[str] = None,
        new_metadata: Optional[Dict] = None,
    ):
        pass

    #
    # ITEM METHODS
    #
    def _add(
        self,
        ids,
        collection_name: str,
        embeddings: Embeddings,
        metadatas: Optional[Metadatas] = None,
        documents: Optional[Documents] = None,
        increment_index: bool = True,
    ):

        topic = self._topic(collection_name)

        for i, e, m, d in zip(
            ids,
            embeddings,
            metadatas or itertools.repeat(None),
            documents or itertools.repeat(None),
        ):

            if d is not None:
                if m is None:
                    m = {"document": d}
                else:
                    m["document"] = d

            if m is not None:
                metadata = {k: str(v) for k, v in m.items()}
            else:
                metadata = None

            embedding = InsertEmbeddingRecord(
                id=i, embedding=e, metadata=metadata, insert_type=InsertType.ADD_ONLY
            )

            self.ingest_impl.submit_embedding(topic_name=topic, embedding=embedding)

    def _update(
        self,
        collection_name: str,
        ids: IDs,
        embeddings: Optional[Embeddings] = None,
        metadatas: Optional[Metadatas] = None,
        documents: Optional[Documents] = None,
    ):
        pass

    # TODO: this could be cached for better performance
    def _get_metadata_reader(self, collection_name: str) -> MetadataReader:
        segment = self.sysdb.get_segments(topic=self._topic(collection_name), scope="metadata")[0]
        impl = self.segment_manager.get_instance(segment)
        return cast(MetadataReader, impl)

    # TODO: this could be cached for better performance
    def _get_vector_reader(self, collection_name: str) -> VectorReader:
        segment = self.sysdb.get_segments(topic=self._topic(collection_name), scope="vector")[0]
        impl = self.segment_manager.get_instance(segment)
        return cast(VectorReader, impl)

    def _count(self, collection_name: str) -> int:
        return self._get_metadata_reader(collection_name).count_metadata()

    def _get(
        self,
        collection_name: str,
        ids: Optional[IDs] = None,
        where: Optional[Where] = {},
        sort: Optional[str] = None,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
        page: Optional[int] = None,
        page_size: Optional[int] = None,
        where_document: Optional[WhereDocument] = {},
        include: Include = ["embeddings", "metadatas", "documents"],
    ) -> GetResult:

        if page is not None:
            if page_size is None:
                raise ValueError("page_size must be specified if page is specified")
            limit = page_size
            offset = page * page_size

        metadata_reader = self._get_metadata_reader(collection_name)
        metadata_records = metadata_reader.get_metadata(
            ids=ids,
            where=where,
            sort=sort,
            limit=limit,
            offset=offset,
            where_document=where_document,
        )
        filtered_ids = [r["id"] for r in metadata_records]

        embeddings = None
        documents = None
        metadatas = None

        if "embeddings" in include:
            vector_reader = self._get_vector_reader(collection_name)
            vector_records = vector_reader.get_vectors(ids=filtered_ids)
            embeddings = [r["embedding"] for r in vector_records]

        if "documents" in include:
            documents = [cast(str, r["metadata"]["document"]) for r in metadata_records]

        if "metadatas" in include:
            for r in metadata_records:
                if "document" in r["metadata"]:
                    del r["metadata"]["document"]

            metadatas = [r["metadata"] for r in metadata_records]

        return GetResult(
            ids=filtered_ids, embeddings=embeddings, metadatas=metadatas, documents=documents
        )

    def _delete(
        self,
        collection_name: str,
        ids: Optional[IDs],
        where: Optional[Where] = {},
        where_document: Optional[WhereDocument] = {},
    ):
        if ids is None:
            metadata_reader = self._get_metadata_reader(collection_name)
            metadata_records = metadata_reader.get_metadata(
                where=where, where_document=where_document
            )
            ids = [r["id"] for r in metadata_records]

        topic = self._topic(collection_name)

        for i in ids:
            self.ingest_impl.submit_embedding_delete(topic_name=topic, id=i)

    def _query(
        self,
        collection_name: str,
        query_embeddings: Embeddings,
        n_results: int = 10,
        where: Where = {},
        where_document: WhereDocument = {},
        include: Include = ["embeddings", "metadatas", "documents", "distances"],
    ) -> QueryResult:
        pass

    def _peek(self, collection_name: str, n: int = 10) -> GetResult:
        pass

    def reset(self) -> bool:
        if self.settings.enable_system_reset:
            self.segment_manager.reset()
            self.sysdb.reset()
            self.ingest_impl.reset()
            return True
        else:
            raise Exception("System reset is disabled")

    def raw_sql(self, sql: str):
        pass

    def create_index(self, collection_name: Optional[str] = None) -> bool:
        pass