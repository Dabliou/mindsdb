import os
import copy
from typing import Dict, List, Optional

import pandas as pd
import hashlib
import numpy as np

from mindsdb_sql_parser.ast import (
    BinaryOperation,
    Constant,
    Identifier,
    Select,
    Update,
    Delete,
    Star
)
from mindsdb_sql_parser.ast.mindsdb import CreatePredictor

from mindsdb.integrations.utilities.query_traversal import query_traversal

import mindsdb.interfaces.storage.db as db
from mindsdb.integrations.libs.vectordatabase_handler import (
    DistanceFunction,
    TableField,
    VectorStoreHandler,
)
from mindsdb.integrations.utilities.rag.rag_pipeline_builder import RAG
from mindsdb.integrations.utilities.rag.config_loader import load_rag_config

from mindsdb.interfaces.agents.constants import DEFAULT_EMBEDDINGS_MODEL_CLASS
from mindsdb.interfaces.agents.langchain_agent import create_chat_model, get_llm_provider
from mindsdb.interfaces.database.projects import ProjectController
from mindsdb.interfaces.knowledge_base.preprocessing.models import PreprocessingConfig, Document
from mindsdb.interfaces.knowledge_base.preprocessing.document_preprocessor import PreprocessorFactory
from mindsdb.interfaces.model.functions import PredictorRecordNotFound
from mindsdb.utilities.exception import EntityExistsError, EntityNotExistsError
from mindsdb.integrations.utilities.sql_utils import FilterCondition, FilterOperator

from mindsdb.api.executor.command_executor import ExecuteCommands
from mindsdb.utilities import log
from mindsdb.integrations.utilities.rag.rerankers.reranker_compressor import LLMReranker

logger = log.getLogger(__name__)

KB_TO_VECTORDB_COLUMNS = {
    'id': 'original_row_id',
    'chunk_id': 'id',
    'chunk_content': 'content'
}


class KnowledgeBaseTable:
    """
    Knowledge base table interface
    Handlers requests to KB table and modifies data in linked vector db table
    """

    def __init__(self, kb: db.KnowledgeBase, session):
        self._kb = kb
        self._vector_db = None
        self.session = session
        self.document_preprocessor = None
        self.document_loader = None
        self.model_params = None

    def configure_preprocessing(self, config: Optional[dict] = None):
        """Configure preprocessing for the knowledge base table"""
        logger.debug(f"Configuring preprocessing with config: {config}")
        self.document_preprocessor = None  # Reset existing preprocessor
        if config is not None:
            preprocessing_config = PreprocessingConfig(**config)
            self.document_preprocessor = PreprocessorFactory.create_preprocessor(preprocessing_config)
            logger.debug(f"Created preprocessor of type: {type(self.document_preprocessor)}")
        else:
            # Always create a default preprocessor if none specified
            self.document_preprocessor = PreprocessorFactory.create_preprocessor()
            logger.debug("Created default preprocessor")

    def select_query(self, query: Select) -> pd.DataFrame:
        """
        Handles select from KB table.
        Replaces content values with embeddings in where clause. Sends query to vector db
        :param query: query to KB table
        :return: dataframe with the result table
        """
        logger.debug(f"Processing select query: {query}")

        # Extract the content query text for potential reranking

        db_handler = self.get_vector_db()

        logger.debug("Replaced content with embeddings in where clause")
        # set table name
        query.from_table = Identifier(parts=[self._kb.vector_database_table])
        logger.debug(f"Set table name to: {self._kb.vector_database_table}")

        requested_kb_columns = []
        for target in query.targets:
            if isinstance(target, Star):
                requested_kb_columns = None
                break
            else:
                requested_kb_columns.append(target.parts[-1].lower())

        query.targets = [
            Identifier(TableField.ID.value),
            Identifier(TableField.CONTENT.value),
            Identifier(TableField.METADATA.value),
            Identifier(TableField.DISTANCE.value),
        ]

        # Get response from vector db
        logger.debug(f"Using vector db handler: {type(db_handler)}")

        # extract values from conditions and prepare for vectordb
        conditions = []
        query_text = None
        reranking_threshold = None
        query_conditions = db_handler.extract_conditions(query.where)
        if query_conditions is not None:
            for item in query_conditions:
                if item.column == "reranking_threshold" and item.op.value == "=":
                    try:
                        reranking_threshold = float(item.value)
                        # Validate range: must be between 0 and 1
                        if not (0 <= reranking_threshold <= 1):
                            raise ValueError(f"reranking_threshold must be between 0 and 1, got: {reranking_threshold}")
                        logger.debug(f"Found reranking_threshold in query: {reranking_threshold}")
                    except (ValueError, TypeError) as e:
                        error_msg = f"Invalid reranking_threshold value: {item.value}. {str(e)}"
                        logger.error(error_msg)
                        raise ValueError(error_msg)
                elif item.column == TableField.CONTENT.value:
                    query_text = item.value

                    # replace content with embeddings
                    conditions.append(FilterCondition(
                        column=TableField.EMBEDDINGS.value,
                        value=self._content_to_embeddings(item.value),
                        op=FilterOperator.EQUAL,
                    ))
                else:
                    conditions.append(item)

        logger.debug(f"Extracted query text: {query_text}")

        self.addapt_conditions_columns(conditions)
        df = db_handler.dispatch_select(query, conditions)
        df = self.addapt_result_columns(df)

        logger.debug(f"Query returned {len(df)} rows")
        logger.debug(f"Columns in response: {df.columns.tolist()}")
        # Check if we have a rerank_model configured in KB params

        df = self.add_relevance(df, query_text, reranking_threshold)

        # filter by targets
        if requested_kb_columns is not None:
            df = df[requested_kb_columns]
        return df

    def add_relevance(self, df, query_text, reranking_threshold=None):
        relevance_column = TableField.RELEVANCE.value

        rerank_model = self._kb.params.get("rerank_model")
        if rerank_model and query_text and len(df) > 0:
            # Use reranker for relevance score
            try:
                logger.info(f"Using reranker model {rerank_model} for relevance calculation")
                reranker_params = {"model": rerank_model}
                # Apply custom filtering threshold if provided
                if reranking_threshold is not None:
                    reranker_params["filtering_threshold"] = reranking_threshold
                    logger.info(f"Using custom filtering threshold: {reranking_threshold}")

                reranker = LLMReranker(**reranker_params)
                # Get documents to rerank
                documents = df['chunk_content'].tolist()
                # Use the get_scores method with disable_events=True
                scores = reranker.get_scores(query_text, documents)
                # Add scores as the relevance column
                df[relevance_column] = scores

                # Filter by threshold
                scores_array = np.array(scores)
                df = df[scores_array > reranker.filtering_threshold]
                logger.debug(f"Applied reranking with model {rerank_model}, threshold: {reranker.filtering_threshold}")
            except Exception as e:
                logger.error(f"Error during reranking: {str(e)}")
                # Fallback to distance-based relevance
                if 'distance' in df.columns:
                    df[relevance_column] = 1 / (1 + df['distance'])
                else:
                    logger.info("No distance or reranker available")

        elif 'distance' in df.columns:
            # Calculate relevance from distance
            logger.info("Calculating relevance from vector distance")
            df[relevance_column] = 1 / (1 + df['distance'])

        else:
            df[relevance_column] = None
            df['distance'] = None
        # Sort by relevance
        df = df.sort_values(by=relevance_column, ascending=False)
        return df

    def addapt_conditions_columns(self, conditions):
        if conditions is None:
            return
        for condition in conditions:
            if condition.column in KB_TO_VECTORDB_COLUMNS:
                condition.column = KB_TO_VECTORDB_COLUMNS[condition.column]

    def addapt_result_columns(self, df):
        col_update = {}
        for kb_col, vec_col in KB_TO_VECTORDB_COLUMNS.items():
            if vec_col in df.columns:
                col_update[vec_col] = kb_col

        df = df.rename(columns=col_update)

        columns = list(df.columns)
        # update id, get from metadata
        df[TableField.ID.value] = df[TableField.METADATA.value].apply(
            lambda m: None if m is None else m.get('original_row_id')
        )

        # id on first place
        return df[[TableField.ID.value] + columns]

    def insert_files(self, file_names: List[str]):
        """Process and insert files"""
        if not self.document_loader:
            raise ValueError("Document loader not configured")

        documents = list(self.document_loader.load_files(file_names))
        if documents:
            self.insert_documents(documents)

    def insert_web_pages(
            self,
            urls: List[str],
            crawl_depth: int,
            limit: int,
            filters: List[str] = None
    ):
        """Process and insert web pages"""
        if not self.document_loader:
            raise ValueError("Document loader not configured")

        documents = list(self.document_loader.load_web_pages(
            urls,
            limit=limit,
            crawl_depth=crawl_depth,
            filters=filters
        ))
        if documents:
            self.insert_documents(documents)

    def insert_query_result(self, query: str, project_name: str):
        """Process and insert SQL query results"""
        if not self.document_loader:
            raise ValueError("Document loader not configured")

        documents = list(self.document_loader.load_query_result(query, project_name))
        if documents:
            self.insert_documents(documents)

    def insert_rows(self, rows: List[Dict]):
        """Process and insert raw data rows"""
        if not rows:
            return

        documents = [Document(
            content=row.get('content', ''),
            id=row.get('id'),
            metadata=row.get('metadata', {})
        ) for row in rows]

        self.insert_documents(documents)

    def insert_documents(self, documents: List[Document]):
        """Process and insert documents with preprocessing if configured"""
        df = pd.DataFrame([doc.model_dump() for doc in documents])

        self.insert(df)

    def update_query(self, query: Update):
        # add embeddings to content in updated collumns
        query = copy.deepcopy(query)

        emb_col = TableField.EMBEDDINGS.value
        cont_col = TableField.CONTENT.value
        if cont_col in query.update_columns:
            content = query.update_columns[cont_col]

            # Apply preprocessing to content if configured
            if self.document_preprocessor:
                doc = Document(
                    content=content.value,
                    metadata={}  # Empty metadata for content-only updates
                )
                processed_chunks = self.document_preprocessor.process_documents([doc])
                if processed_chunks:
                    content.value = processed_chunks[0].content

            query.update_columns[emb_col] = Constant(self._content_to_embeddings(content))

        # TODO search content in where clause?

        # set table name
        query.table = Identifier(parts=[self._kb.vector_database_table])

        # send to vectordb
        db_handler = self.get_vector_db()
        conditions = db_handler.extract_conditions(query.where)
        self.addapt_conditions_columns(conditions)
        db_handler.dispatch_update(query, conditions)

    def delete_query(self, query: Delete):
        """
        Handles delete query to KB table.
        Replaces content values with embeddings in WHERE clause. Sends query to vector db
        :param query: query to KB table
        """
        query_traversal(query.where, self._replace_query_content)

        # set table name
        query.table = Identifier(parts=[self._kb.vector_database_table])

        # send to vectordb
        db_handler = self.get_vector_db()
        conditions = db_handler.extract_conditions(query.where)
        self.addapt_conditions_columns(conditions)
        db_handler.dispatch_delete(query, conditions)

    def hybrid_search(
        self,
        query: str,
        keywords: List[str] = None,
        metadata: Dict[str, str] = None,
        distance_function=DistanceFunction.COSINE_DISTANCE
    ) -> pd.DataFrame:
        query_df = pd.DataFrame.from_records([{TableField.CONTENT.value: query}])
        embeddings_df = self._df_to_embeddings(query_df)
        if embeddings_df.empty:
            return pd.DataFrame([])
        embeddings = embeddings_df.iloc[0][TableField.EMBEDDINGS.value]
        keywords_query = None
        if keywords is not None:
            keywords_query = ' '.join(keywords)
        db_handler = self.get_vector_db()
        return db_handler.hybrid_search(
            self._kb.vector_database_table,
            embeddings,
            query=keywords_query,
            metadata=metadata,
            distance_function=distance_function
        )

    def clear(self):
        """
        Clear data in KB table
        Sends delete to vector db table
        """
        db_handler = self.get_vector_db()
        db_handler.delete(self._kb.vector_database_table)

    def insert(self, df: pd.DataFrame):
        """Insert dataframe to KB table."""
        if df.empty:
            return

        # First adapt column names to identify content and metadata columns
        adapted_df = self._adapt_column_names(df)
        content_columns = self._kb.params.get('content_columns', [TableField.CONTENT.value])

        # Convert DataFrame rows to documents, creating separate documents for each content column
        raw_documents = []
        for idx, row in adapted_df.iterrows():
            base_metadata = self._parse_metadata(row.get(TableField.METADATA.value, {}))
            provided_id = row.get(TableField.ID.value)

            for col in content_columns:
                content = row.get(col)
                if content and str(content).strip():
                    content_str = str(content)

                    # Use provided_id directly if it exists, otherwise generate one
                    doc_id = self._generate_document_id(content_str, col, provided_id)

                    # Need provided ID to link chunks back to original source (e.g. database row).
                    row_id = provided_id if provided_id else idx

                    metadata = {
                        **base_metadata,
                        'original_row_id': str(row_id),
                        'content_column': col,
                    }

                    raw_documents.append(Document(
                        content=content_str,
                        id=doc_id,
                        metadata=metadata
                    ))

        # Apply preprocessing to all documents if preprocessor exists
        if self.document_preprocessor:
            processed_chunks = self.document_preprocessor.process_documents(raw_documents)
        else:
            processed_chunks = raw_documents  # Use raw documents if no preprocessing

        # Convert processed chunks back to DataFrame with standard structure
        df = pd.DataFrame([{
            TableField.CONTENT.value: chunk.content,
            TableField.ID.value: chunk.id,
            TableField.METADATA.value: chunk.metadata
        } for chunk in processed_chunks])

        if df.empty:
            logger.warning("No valid content found in any content columns")
            return

        # add embeddings and send to vector db
        df_emb = self._df_to_embeddings(df)
        df = pd.concat([df, df_emb], axis=1)
        db_handler = self.get_vector_db()
        db_handler.do_upsert(self._kb.vector_database_table, df)

    def _adapt_column_names(self, df: pd.DataFrame) -> pd.DataFrame:
        '''
        Convert input columns for vector db input
        - id, content and metadata
        '''
        # Debug incoming data
        logger.debug(f"Input DataFrame columns: {df.columns}")
        logger.debug(f"Input DataFrame first row: {df.iloc[0].to_dict()}")

        params = self._kb.params
        columns = list(df.columns)

        # -- prepare id --
        id_column = params.get('id_column')
        if id_column is not None and id_column not in columns:
            id_column = None

        if id_column is None and TableField.ID.value in columns:
            id_column = TableField.ID.value

        # Also check for case-insensitive 'id' column
        if id_column is None:
            column_map = {col.lower(): col for col in columns}
            if 'id' in column_map:
                id_column = column_map['id']

        if id_column is not None:
            columns.remove(id_column)
            logger.debug(f"Using ID column: {id_column}")

        # Create output dataframe
        df_out = pd.DataFrame()

        # Add ID if present
        if id_column is not None:
            df_out[TableField.ID.value] = df[id_column]
            logger.debug(f"Added IDs: {df_out[TableField.ID.value].tolist()}")

        # -- prepare content and metadata --
        content_columns = params.get('content_columns', [TableField.CONTENT.value])
        metadata_columns = params.get('metadata_columns')

        logger.debug(f"Processing with: content_columns={content_columns}, metadata_columns={metadata_columns}")

        # Handle SQL query result columns
        if content_columns:
            # Ensure content columns are case-insensitive
            column_map = {col.lower(): col for col in columns}
            content_columns = [
                column_map.get(col.lower(), col)
                for col in content_columns
            ]
            logger.debug(f"Mapped content columns: {content_columns}")

        if metadata_columns:
            # Ensure metadata columns are case-insensitive
            column_map = {col.lower(): col for col in columns}
            metadata_columns = [
                column_map.get(col.lower(), col)
                for col in metadata_columns
            ]
            logger.debug(f"Mapped metadata columns: {metadata_columns}")

        if content_columns is not None:
            content_columns = list(set(content_columns).intersection(columns))
            if len(content_columns) == 0:
                raise ValueError(f'Content columns {params.get("content_columns")} not found in dataset: {columns}')

            if metadata_columns is not None:
                metadata_columns = list(set(metadata_columns).intersection(columns))
            else:
                # all the rest columns
                metadata_columns = list(set(columns).difference(content_columns))

        # Add content columns directly (don't combine them)
        for col in content_columns:
            df_out[col] = df[col]

        # Add metadata
        if metadata_columns and len(metadata_columns) > 0:
            def convert_row_to_metadata(row):
                metadata = {}
                for col in metadata_columns:
                    value = row[col]
                    # Convert numpy/pandas types to Python native types
                    if pd.api.types.is_datetime64_any_dtype(value) or isinstance(value, pd.Timestamp):
                        value = str(value)
                    elif pd.api.types.is_integer_dtype(value):
                        value = int(value)
                    elif pd.api.types.is_float_dtype(value):
                        value = float(value)
                    elif pd.api.types.is_bool_dtype(value):
                        value = bool(value)
                    elif isinstance(value, dict):
                        metadata.update(value)
                        continue
                    else:
                        value = str(value)
                    metadata[col] = value
                return metadata

            metadata_dict = df[metadata_columns].apply(convert_row_to_metadata, axis=1)
            df_out[TableField.METADATA.value] = metadata_dict

        logger.debug(f"Output DataFrame columns: {df_out.columns}")
        logger.debug(f"Output DataFrame first row: {df_out.iloc[0].to_dict() if not df_out.empty else 'Empty'}")

        return df_out

    def _replace_query_content(self, node, **kwargs):
        if isinstance(node, BinaryOperation):
            if isinstance(node.args[0], Identifier) and isinstance(node.args[1], Constant):
                col_name = node.args[0].parts[-1]
                if col_name.lower() == TableField.CONTENT.value:
                    # replace
                    node.args[0].parts = [TableField.EMBEDDINGS.value]
                    node.args[1].value = [self._content_to_embeddings(node.args[1].value)]

    def get_vector_db(self) -> VectorStoreHandler:
        """
        helper to get vector db handler
        """
        if self._vector_db is None:
            database = db.Integration.query.get(self._kb.vector_database_id)
            if database is None:
                raise ValueError('Vector database not found. Is it deleted?')
            database_name = database.name
            self._vector_db = self.session.integration_controller.get_data_handler(database_name)
        return self._vector_db

    def get_vector_db_table_name(self) -> str:
        """
        helper to get underlying table name used for embeddings
        """
        return self._kb.vector_database_table

    def _df_to_embeddings(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Returns embeddings for input dataframe.
        Uses model embedding model to convert content to embeddings.
        Automatically detects input and output of model using model description
        :param df:
        :return: dataframe with embeddings
        """

        if df.empty:
            return pd.DataFrame([], columns=[TableField.EMBEDDINGS.value])

        model_id = self._kb.embedding_model_id
        # get the input columns
        model_rec = db.session.query(db.Predictor).filter_by(id=model_id).first()

        assert model_rec is not None, f"Model not found: {model_id}"
        model_project = db.session.query(db.Project).filter_by(id=model_rec.project_id).first()

        project_datanode = self.session.datahub.get(model_project.name)

        # keep only content
        df = df[[TableField.CONTENT.value]]

        model_using = model_rec.learn_args.get('using', {})
        input_col = model_using.get('question_column')
        if input_col is None:
            input_col = model_using.get('input_column')

        if input_col is not None and input_col != TableField.CONTENT.value:
            df = df.rename(columns={TableField.CONTENT.value: input_col})

        df_out = project_datanode.predict(
            model_name=model_rec.name,
            df=df,
            params=self.model_params
        )

        target = model_rec.to_predict[0]
        if target != TableField.EMBEDDINGS.value:
            # adapt output for vectordb
            df_out = df_out.rename(columns={target: TableField.EMBEDDINGS.value})
        df_out = df_out[[TableField.EMBEDDINGS.value]]

        return df_out

    def _content_to_embeddings(self, content: str) -> List[float]:
        """
        Converts string to embeddings
        :param content: input string
        :return: embeddings
        """
        df = pd.DataFrame([[content]], columns=[TableField.CONTENT.value])
        res = self._df_to_embeddings(df)
        return res[TableField.EMBEDDINGS.value][0]

    def build_rag_pipeline(self, retrieval_config: dict):
        """
        Builds a RAG pipeline with returned sources

        Args:
            retrieval_config: dict with retrieval config

        Returns:
            RAG: Configured RAG pipeline instance

        Raises:
            ValueError: If the configuration is invalid or required components are missing
        """
        # Get embedding model from knowledge base
        embeddings_model = None
        if self._kb.embedding_model:
            # Extract embedding model args from knowledge base table
            embedding_args = self._kb.embedding_model.learn_args.get('using', {})
            # Construct the embedding model directly
            from mindsdb.integrations.handlers.langchain_embedding_handler.langchain_embedding_handler import construct_model_from_args
            embeddings_model = construct_model_from_args(embedding_args)
            logger.debug(f"Using knowledge base embedding model with args: {embedding_args}")
        else:
            embeddings_model = DEFAULT_EMBEDDINGS_MODEL_CLASS()
            logger.debug("Using default embedding model as knowledge base has no embedding model")

        # Update retrieval config with knowledge base parameters
        kb_params = {
            'vector_store_config': {
                'kb_table': self
            }
        }

        # Load and validate config
        try:
            rag_config = load_rag_config(retrieval_config, kb_params, embeddings_model)

            # Build LLM if specified
            if 'llm_model_name' in rag_config:
                llm_args = {"model_name": rag_config.llm_model_name}
                if not rag_config.llm_provider:
                    llm_args['provider'] = get_llm_provider(llm_args)
                else:
                    llm_args["provider"] = rag_config.llm_provider
                rag_config.llm = create_chat_model(llm_args)

            # Create RAG pipeline
            rag = RAG(rag_config)
            logger.debug(f"RAG pipeline created with config: {rag_config}")
            return rag

        except Exception as e:
            logger.error(f"Error building RAG pipeline: {str(e)}")
            raise ValueError(f"Failed to build RAG pipeline: {str(e)}")

    def _parse_metadata(self, base_metadata):
        """Helper function to robustly parse metadata string to dict"""
        if isinstance(base_metadata, dict):
            return base_metadata
        if isinstance(base_metadata, str):
            try:
                import ast
                return ast.literal_eval(base_metadata)
            except (SyntaxError, ValueError):
                logger.warning(f"Could not parse metadata: {base_metadata}. Using empty dict.")
                return {}
        return {}

    def _generate_document_id(self, content: str, content_column: str, provided_id: str = None) -> str:
        """
        Generate a deterministic document ID from content and column name.
        If provided_id exists, combines it with content_column.

        Args:
            content: The content string
            content_column: Name of the content column
            provided_id: Optional user-provided ID
        Returns:
            Deterministic document ID
        """
        if provided_id is not None:
            return f"{provided_id}_{content_column}"

        id_string = f"content={content}_column={content_column}"
        return hashlib.sha256(id_string.encode()).hexdigest()

    def _convert_metadata_value(self, value):
        """
        Convert metadata value to appropriate Python type.

        Args:
            value: The value to convert

        Returns:
            Converted value in appropriate Python type
        """
        if pd.isna(value):
            return None

        # Handle pandas/numpy types
        if pd.api.types.is_datetime64_any_dtype(value) or isinstance(value, pd.Timestamp):
            return str(value)
        elif pd.api.types.is_integer_dtype(type(value)):
            return int(value)
        elif pd.api.types.is_float_dtype(type(value)):
            return float(value)
        elif pd.api.types.is_bool_dtype(type(value)):
            return bool(value)

        # Handle basic Python types
        if isinstance(value, (int, float, bool)):
            return value

        # Convert everything else to string
        return str(value)


class KnowledgeBaseController:
    """
    Knowledge base controller handles all
    manages knowledge bases
    """

    def __init__(self, session) -> None:
        self.session = session

    def add(
            self,
            name: str,
            project_name: str,
            embedding_model: Identifier,
            storage: Identifier,
            params: dict,
            preprocessing_config: Optional[dict] = None,
            if_not_exists: bool = False
    ) -> db.KnowledgeBase:
        """
        Add a new knowledge base to the database
        :param preprocessing_config: Optional preprocessing configuration to validate and store
        :param is_sparse: Whether to use sparse vectors for embeddings
        :param vector_size: Optional size specification for vectors, required when is_sparse=True
        """
        # Validate preprocessing config first if provided
        if preprocessing_config is not None:
            PreprocessingConfig(**preprocessing_config)  # Validate before storing
            params = params or {}
            params['preprocessing'] = preprocessing_config

        # Check if vector_size is provided when using sparse vectors
        is_sparse = params.get('is_sparse')
        vector_size = params.get('vector_size')
        if is_sparse and vector_size is None:
            raise ValueError("vector_size is required when is_sparse=True")

        # get project id
        project = self.session.database_controller.get_project(project_name)
        project_id = project.id

        # not difference between cases in sql
        name = name.lower()
        # check if knowledge base already exists
        kb = self.get(name, project_id)
        if kb is not None:
            if if_not_exists:
                return kb
            raise EntityExistsError("Knowledge base already exists", name)

        if embedding_model is None:
            # create default embedding model
            model_name = self._get_default_embedding_model(project.name, params=params)
            params['default_embedding_model'] = model_name
        else:
            # get embedding model from input
            model_name = embedding_model.parts[-1]

        if embedding_model is not None and len(embedding_model.parts) > 1:
            # model project is set
            model_project = self.session.database_controller.get_project(embedding_model.parts[-2])
        else:
            model_project = project

        model = self.session.model_controller.get_model(
            name=model_name,
            project_name=model_project.name
        )
        model_record = db.Predictor.query.get(model['id'])
        embedding_model_id = model_record.id

        # search for the vector database table
        if storage is None:
            cloud_pg_vector = os.environ.get('KB_PGVECTOR_URL')
            if cloud_pg_vector:
                vector_table_name = name
                # Add sparse vector support for pgvector
                vector_db_params = {}
                # Check both explicit parameter and model configuration
                is_sparse = is_sparse or model_record.learn_args.get('using', {}).get('sparse')
                if is_sparse:
                    vector_db_params['is_sparse'] = True
                    if vector_size is not None:
                        vector_db_params['vector_size'] = vector_size
                vector_db_name = self._create_persistent_pgvector(vector_db_params)

            else:
                # create chroma db with same name
                vector_table_name = "default_collection"
                vector_db_name = self._create_persistent_chroma(name)
                # memorize to remove it later
                params['default_vector_storage'] = vector_db_name
        elif len(storage.parts) != 2:
            raise ValueError('Storage param has to be vector db with table')
        else:
            vector_db_name, vector_table_name = storage.parts

        # create table in vectordb before creating KB
        self.session.datahub.get(vector_db_name).integration_handler.create_table(
            vector_table_name
        )
        vector_database_id = self.session.integration_controller.get(vector_db_name)['id']

        # Store sparse vector settings in params if specified
        if is_sparse:
            params = params or {}
            params['vector_config'] = {
                'is_sparse': is_sparse
            }
            if vector_size is not None:
                params['vector_config']['vector_size'] = vector_size

        kb = db.KnowledgeBase(
            name=name,
            project_id=project_id,
            vector_database_id=vector_database_id,
            vector_database_table=vector_table_name,
            embedding_model_id=embedding_model_id,
            params=params,
        )
        db.session.add(kb)
        db.session.commit()
        return kb

    def _create_persistent_pgvector(self, params=None):
        """Create default vector database for knowledge base, if not specified"""
        vector_store_name = "kb_pgvector_store"

        # check if exists
        if self.session.integration_controller.get(vector_store_name):
            return vector_store_name

        self.session.integration_controller.add(vector_store_name, 'pgvector', params or {})
        return vector_store_name

    def _create_persistent_chroma(self, kb_name, engine="chromadb"):
        """Create default vector database for knowledge base, if not specified"""

        vector_store_name = f"{kb_name}_{engine}"

        vector_store_folder_name = f"{vector_store_name}"
        connection_args = {"persist_directory": vector_store_folder_name}

        # check if exists
        if self.session.integration_controller.get(vector_store_name):
            return vector_store_name

        self.session.integration_controller.add(vector_store_name, engine, connection_args)
        return vector_store_name

    def _get_default_embedding_model(self, project_name, engine="langchain_embedding", params: dict = None):
        """create a default embedding model for knowledge base, if not specified"""
        model_name = "kb_default_embedding_model"

        # check exists
        try:
            model = self.session.model_controller.get_model(model_name, project_name=project_name)
            if model is not None:
                return model_name
        except PredictorRecordNotFound:
            pass

        using_args = {
            'engine': engine
        }
        if engine == 'langchain_embedding':
            # Use default embeddings.
            using_args['class'] = 'openai'

        # Include API key if provided.
        using_args.update({k: v for k, v in params.items() if 'api_key' in k})
        statement = CreatePredictor(
            name=Identifier(parts=[project_name, model_name]),
            using=using_args,
            targets=[
                Identifier(parts=[TableField.EMBEDDINGS.value])
            ]
        )

        command_executor = ExecuteCommands(self.session)
        command_executor.answer_create_predictor(statement, project_name)

        return model_name

    def delete(self, name: str, project_name: int, if_exists: bool = False) -> None:
        """
        Delete a knowledge base from the database
        """
        try:
            project = self.session.database_controller.get_project(project_name)
        except ValueError:
            raise ValueError(f"Project not found: {project_name}")
        project_id = project.id

        # check if knowledge base exists
        kb = self.get(name, project_id)
        if kb is None:
            # knowledge base does not exist
            if if_exists:
                return
            else:
                raise EntityNotExistsError("Knowledge base does not exist", name)

        # kb exists
        db.session.delete(kb)
        db.session.commit()

        # drop objects if they were created automatically
        if 'default_vector_storage' in kb.params:
            try:
                handler = self.session.datahub.get(kb.params['default_vector_storage']).integration_handler
                handler.drop_table(kb.vector_database_table)
                self.session.integration_controller.delete(kb.params['default_vector_storage'])
            except EntityNotExistsError:
                pass
        if 'default_embedding_model' in kb.params:
            try:
                self.session.model_controller.delete_model(kb.params['default_embedding_model'], project_name)
            except EntityNotExistsError:
                pass

    def get(self, name: str, project_id: int) -> db.KnowledgeBase:
        """
        Get a knowledge base from the database
        by name + project_id
        """
        kb = (
            db.session.query(db.KnowledgeBase)
            .filter_by(
                name=name,
                project_id=project_id,
            )
            .first()
        )
        return kb

    def get_table(self, name: str, project_id: int, params: dict = None) -> KnowledgeBaseTable:
        """
        Returns kb table object with properly configured preprocessing
        :param name: table name
        :param project_id: project id
        :param params: runtime parameters for KB. Keys: 'model' - parameters for embedding model
        :return: kb table object
        """
        kb = self.get(name, project_id)
        if kb is not None:
            table = KnowledgeBaseTable(kb, self.session)
            if params:
                table.model_params = params.get('model')

            # Always configure preprocessing - either from params or default
            if kb.params and 'preprocessing' in kb.params:
                table.configure_preprocessing(kb.params['preprocessing'])
            else:
                table.configure_preprocessing(None)  # This ensures default preprocessor is created

            return table

    def list(self, project_name: str = None) -> List[dict]:
        """
        List all knowledge bases from the database
        belonging to a project
        """
        project_controller = ProjectController()
        projects = project_controller.get_list()
        if project_name is not None:
            projects = [p for p in projects if p.name == project_name]

        query = (
            db.session.query(db.KnowledgeBase)
            .filter(db.KnowledgeBase.project_id.in_(list([p.id for p in projects])))
        )

        data = []
        project_names = {
            i.id: i.name
            for i in project_controller.get_list()
        }

        for record in query:
            vector_database = record.vector_database
            embedding_model = record.embedding_model

            data.append({
                'id': record.id,
                'name': record.name,
                'project_id': record.project_id,
                'project_name': project_names[record.project_id],
                'embedding_model': embedding_model.name if embedding_model is not None else None,
                'vector_database': None if vector_database is None else vector_database.name,
                'vector_database_table': record.vector_database_table,
                'params': record.params
            })

        return data

    def update(self, name: str, project_id: int, **kwargs) -> db.KnowledgeBase:
        """
        Update a knowledge base record
        """
        raise NotImplementedError()
