import os
from fastapi.responses import FileResponse
from langchain.text_splitter import RecursiveCharacterTextSplitter
from fastapi import Depends, UploadFile, File
from fastapi.routing import APIRouter
from sqlalchemy.orm import Session
from datetime import datetime
from uuid import uuid4
from typing import List
import chromadb
import logging
import traceback
from model_server.database.database_models import Course, Document, User
from model_server.chat.model import HTTPErrorResponse
from model_server.database.database import get_db
from model_server.deps import get_current_user
from model_server.config import logging_level
from .model import DocumentListParams, DocumentResult, ExtractionResult, AddSubjectParams, GetCourseParams, ReturnDocumentList
from .util import pdf_extraction_alg, pptx_extraction_alg


ROOT_DIR = os.getcwd()


class Embedder:
    router: APIRouter

    def __init__(self):

        self.logger = logging.getLogger(f"{__name__}")
        logging.basicConfig()
        self.logger.setLevel(logging_level)

        self.router = APIRouter(
            tags=["Embeddings"]
        )
        self.router.add_api_route(
            "/",
            endpoint=self.embed_file,
            methods=["POST"],
            responses={
                200: {"model": ExtractionResult},
                401: {"model": HTTPErrorResponse},
                403: {"model": HTTPErrorResponse},
                404: {"model": HTTPErrorResponse}
            },
        )

        self.router.add_api_route(
            "/documents",
            endpoint=self.list_documents,
            methods=["POST"],
            responses={
                200: {"model": DocumentResult},
                401: {"model": HTTPErrorResponse},
                403: {"model": HTTPErrorResponse},
                404: {"model": HTTPErrorResponse}
            },
        )

        self.router.add_api_route(
            "/courses",
            endpoint=self.get_all_subjects,
            methods=["GET"],
            responses={
                200: {"model": DocumentResult},
                401: {"model": HTTPErrorResponse},
                403: {"model": HTTPErrorResponse},
                404: {"model": HTTPErrorResponse}
            },
        )

        self.router.add_api_route(
            "/courses/add",
            endpoint=self.add_subject,
            methods=["POST"],
            responses={
                200: {"model": DocumentResult},
                401: {"model": HTTPErrorResponse},
                403: {"model": HTTPErrorResponse},
                404: {"model": HTTPErrorResponse}
            },
        )

        self.router.add_api_route(
            "/documents/{course_code}/{filename}",
            endpoint=self.search_documents,
            methods=["GET"]
        )

        self.router.add_api_route(
            "/documents/delete/{course_code}/{filename}",
            endpoint=self.delete_document,
            methods=["GET"]
        )
        # DEPRECATED
        # self.router.add_api_route(
        #     "/courses/get_code",
        #     endpoint=self.get_course_code,
        #     methods=["POST"],
        #     responses={
        #         200: {"model": DocumentResult},
        #         401: {"model": HTTPErrorResponse},
        #         403: {"model": HTTPErrorResponse},
        #         404: {"model": HTTPErrorResponse}
        #     },
        # )

        # self.router.add_api_route(
        #     "/test",
        #     endpoint=self.query,
        #     methods=["POST"],
        # )

        self.logger.info("initialized embeddings route")

        self._client = chromadb.PersistentClient(path="./chroma-vectorstore")
        self._collection = self._client.get_or_create_collection(
            "vectorstore-18-1-24"
            )
        self.logger.debug("Initialized chromadb")

    async def embed_file(
        self,
        files: List[UploadFile] = File(...),
        user: User = Depends(get_current_user),
        db: Session = Depends(get_db)
    ):

        result_list = []
        self.logger.debug(f"recieved {len(files)} files")
        for file in files:
            result_dict = {
                "filename": file.filename[8:],  # type: ignore
                "course_code": file.filename[:8]  # type: ignore
            }

            try:
                if str(file.filename)[-4:] == ".pdf":
                    result = await pdf_extraction_alg(file)

                elif str(file.filename)[-5:] == ".pptx":
                    result = await pptx_extraction_alg(file)

                else:
                    result = ""
                    self.logger.debug("Placeholder result selected")

                result_dict["content"] = result

                self.logger.debug(
                    f"extracted {len(result)} characters from {file.filename}"
                    )

                check_embeddings = db.query(Document).filter(Document.course_code == result_dict['course_code']).filter(Document.document_name == result_dict['filename']).first()  # type: ignore
                # type: ignore

                if check_embeddings is not None:
                    self.logger.info("Document already present")
                    return HTTPErrorResponse(
                        detail="Embedding present"
                    )

                self.add_document(result_dict, db, user)

                result_list.append(result_dict)

            except Exception:
                self.logger.info(traceback.format_exc())
                self.logger.error("Embedding error")

        return ExtractionResult(
                results=f"{len(result_list)} files added to vectorstore."
            )

    def add_document(self, extracted, db, user):
        self.logger.debug("Initializing Text splitter")
        text_splitter = RecursiveCharacterTextSplitter(
            chunk_size=800,
            chunk_overlap=200,
            length_function=len,
            is_separator_regex=False,
        )

        self.logger.debug("Creating documents of 1500 chars")

        docs = text_splitter.create_documents([extracted['content']])

        self.logger.debug(
            f"Adding {len(docs)} documents of 1500 chars to vectorstore"
            )

        self._collection.add(
            documents=[d.page_content for d in docs],
            metadatas=[{
                "source": extracted['filename'],
                "course_code": extracted['course_code']
                } for d in docs],
            ids=[
                (f"{extracted['course_code']}-{extracted['filename']}-"
                    + str(i)) for i in range(len(docs))],
        )

        self.logger.info(f"{extracted['filename']} added to embeddings")

        db.add(Document(
            id=str(uuid4()),
            added_at=datetime.now(),
            user_id=user.id,
            course_code=extracted['course_code'],
            document_name=extracted['filename']
        ))  # type: ignore
        db.commit()

    def delete_document_from_vectorstore(self, doc_name):
        self._collection.delete(
            where={"source": doc_name}
        )

    def query(self, message, course_id):

        results = self._collection.query(
            query_texts=[message],
            n_results=1,
            where={"course_code": course_id},
        )

        if len(results['distances'][0]) != 0:  # type: ignore
            self.logger.debug(f"Confidence: {results}")
            if results['distances'][0][0] < 1.0:  # type: ignore
                return (
                    str(results['documents'][0][0]),  # type: ignore
                    str(results['metadatas'][0][0]['source'])  # type: ignore
                )

        return ("", "")

    def list_documents(
        self,
        data: DocumentListParams,
        db: Session = Depends(get_db),
        user: User = Depends(get_current_user),
    ):
        course_code = self.get_course_code(GetCourseParams(subject_name=data.subject), db)
        self.logger.debug(f"course code: {course_code}")
        result = db.query(Document).filter(Document.course_code == course_code).all()  # type: ignore
        return ReturnDocumentList(documents=result, course_code=course_code)

    def get_all_subjects(
        self,
        db: Session = Depends(get_db),
        user: User = Depends(get_current_user)
    ):
        result = db.query(Course).all()
        response = [course.subject_name for course in result]
        return {"courses": response}

    def add_subject(
        self,
        params: AddSubjectParams,
        db: Session = Depends(get_db),
        user: User = Depends(get_current_user)
    ):
        db.add(Course(
            id=params.course_code,
            subject_name=params.subject_name
        ))  # type: ignore
        db.commit()
        return {"message": "success"}

    def get_course_code(
        self,
        params: GetCourseParams,
        db: Session,
        user: User = Depends(get_current_user)
    ):
        result = db.query(Course).filter(Course.subject_name == params.subject_name).first()  # type: ignore
        if result is None:
            return ""
        return result.id

    def search_documents(self, course_code, filename):
        try:
            path = os.path.join(ROOT_DIR, "documents", course_code, filename)

            if path[-3:] == "pdf":
                response = FileResponse(path, media_type="application/pdf")
            elif path[-4:] == "pptx":
                response = FileResponse(path, media_type="application/vnd.openxmlformats-officedocument.presentationml.presentation")
            else:
                response = FileResponse(path, media_type="application/vnd.ms-powerpoint")
            return response
        except Exception:
            return

    def delete_document(
        self,
        course_code,
        filename,
        db: Session = Depends(get_db),
    ):
        try:
            self.logger.debug(f"1course code: {course_code}")
            db.query(Document).filter(Document.document_name == filename).delete()  # type: ignore
            db.commit()
            self.logger.debug(f"2course code: {course_code}")
            self.delete_document_from_vectorstore(filename)
            path = os.path.join(ROOT_DIR, "documents", course_code, filename)
            os.remove(path)
            return "success"
        except Exception:
            return "failed"


embedder = Embedder()
