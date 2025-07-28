"""
查询服务，回答用户的问题
实现混合检索（向量检索 + BM25 关键词检索）和重排序
然后根据检索到的文本快块生成相应的回答
"""

import time
import logging
from typing import List, Dict, Any, Optional, Tuple
from datetime import datetime
from sqlalchemy.orm import Session
from rank_bm25 import BM25Okapi

from ..core.config import settings
from ..db.session import get_db_session
from ..db.models import AnalysisSession, QueryLog, TaskStatus
from ..services.embedding_manager import EmbeddingManager, EmbeddingConfig
from ..services.llm_manager import LLMManager, LLMConfig
from ..services.vector_store import get_vector_store
from ..schemas.repository import (
    QueryRequest, QueryResponse, RetrievedChunk,
    GenerationMode, LLMConfig as LLMConfigSchema
)

logger = logging.getLogger(__name__)


class QueryService:
    """查询服务"""

    def __init__(self):
        self._bm25_cache = {}  # 缓存 BM25 索引
        self._documents_cache = {}  # 缓存文档内容
    
    def clear_cache(self, session_id: str = None):
        """
        清除BM25缓存
        
        Args:
            session_id: 指定会话ID，如果为None则清除所有缓存
        """
        if session_id:
            self._bm25_cache.pop(session_id, None)
            self._documents_cache.pop(session_id, None)
            logger.info(f"🧹 [缓存清除] 已清除会话 {session_id} 的BM25缓存")
        else:
            self._bm25_cache.clear()
            self._documents_cache.clear()
            logger.info(f"🧹 [缓存清除] 已清除所有BM25缓存")

    def query(self, request: QueryRequest) -> QueryResponse:
        """
        处理查询请求

        Args:
            request: 查询请求

        Returns:
            QueryResponse: 查询响应
        """
        start_time = time.time()
        db = get_db_session()

        try:
            # 添加详细的调试日志
            logger.info(f"🔍 [DEBUG] QueryRequest 对象类型和内容:")
            logger.info(f"🔍 [DEBUG] - session_id: {request.session_id} (type: {type(request.session_id)})")
            logger.info(f"🔍 [DEBUG] - question: {request.question[:50]}... (type: {type(request.question)})")
            logger.info(f"🔍 [DEBUG] - generation_mode: {request.generation_mode} (type: {type(request.generation_mode)})")
            logger.info(f"🔍 [DEBUG] - llm_config: {request.llm_config} (type: {type(request.llm_config)})")
            
            if request.llm_config:
                logger.info(f"🔍 [DEBUG] LLMConfig 详细信息:")
                logger.info(f"🔍 [DEBUG] - provider: {request.llm_config.provider} (type: {type(request.llm_config.provider)})")
                logger.info(f"🔍 [DEBUG] - model_name: {request.llm_config.model_name} (type: {type(request.llm_config.model_name)})")
                if hasattr(request.llm_config.provider, 'value'):
                    logger.info(f"🔍 [DEBUG] - provider.value: {request.llm_config.provider.value}")
                else:
                    logger.info(f"🔍 [DEBUG] - provider 没有 .value 属性")
            
            # 验证会话
            session = self._validate_session(db, request.session_id)
            if not session:
                return QueryResponse(
                    answer="会话不存在或分析未完成",
                    generation_mode=request.generation_mode
                )

            logger.info(f"🚀 [查询开始] 会话ID: {request.session_id} - 问题: {request.question[:100]}{'...' if len(request.question) > 100 else ''}")
            logger.info(f"⚙️ [查询配置] 会话ID: {request.session_id} - 生成模式: {request.generation_mode}")
            
            # 执行混合检索
            logger.info(f"🔍 [检索阶段] 会话ID: {request.session_id} - 开始执行混合检索")
            retrieval_start = time.time()
            retrieved_chunks = self._hybrid_retrieval(
                session.session_id,
                session.embedding_config,
                request.question
            )
            retrieval_time = int((time.time() - retrieval_start) * 1000)
            logger.info(f"✅ [检索完成] 会话ID: {request.session_id} - 检索耗时: {retrieval_time}ms, 获得 {len(retrieved_chunks)} 个上下文")

            # 准备响应
            response = QueryResponse(
                retrieved_context=retrieved_chunks,
                generation_mode=request.generation_mode,
                retrieval_time=retrieval_time
            )

            # 根据生成模式处理
            if request.generation_mode == "service" and request.llm_config:
                # 服务端生成答案
                logger.info(f"🤖 [生成阶段] 会话ID: {request.session_id} - 开始使用LLM生成答案")
                generation_start = time.time()
                answer = self._generate_answer(
                    request.question,
                    retrieved_chunks,
                    request.llm_config
                )
                generation_time = int((time.time() - generation_start) * 1000)
                logger.info(f"✅ [生成完成] 会话ID: {request.session_id} - 生成耗时: {generation_time}ms, 答案长度: {len(answer)} 字符")

                response.answer = answer
                response.generation_time = generation_time
            else:
                logger.info(f"📤 [插件模式] 会话ID: {request.session_id} - 仅返回检索上下文，不生成答案")

            response.total_time = int((time.time() - start_time) * 1000)
            logger.info(f"🎉 [查询完成] 会话ID: {request.session_id} - 总耗时: {response.total_time}ms")

            # 记录查询日志
            self._log_query(
                db, request, response, retrieved_chunks
            )

            return response

        except Exception as e:
            logger.error(f"查询处理失败: {str(e)}")
            return QueryResponse(
                answer=f"查询处理失败: {str(e)}",
                generation_mode=request.generation_mode,
                total_time=int((time.time() - start_time) * 1000)
            )

        finally:
            if db:
                db.close()

    def _validate_session(self, db: Session, session_id: str) -> Optional[AnalysisSession]:
        """
        验证会话状态

        Args:
            db: 数据库会话
            session_id: 会话 ID

        Returns:
            Optional[AnalysisSession]: 会话对象或 None
        """
        session = db.query(AnalysisSession).filter(
            AnalysisSession.session_id == session_id
        ).first()

        if not session:
            logger.warning(f"会话不存在: {session_id}")
            return None

        if session.status != TaskStatus.SUCCESS:
            logger.warning(f"会话分析未完成: {session_id}, 状态: {session.status}")
            return None

        return session

    def _hybrid_retrieval(
            self,
            session_id: str,
            embedding_config: Dict[str, Any],
            question: str
    ) -> List[RetrievedChunk]:
        """
        混合检索：向量检索 + BM25 关键词检索

        Args:
            session_id: 会话 ID
            embedding_config: Embedding 配置
            question: 用户问题

        Returns:
            List[RetrievedChunk]: 检索结果
        """
        logger.info(f"🔍 [混合检索开始] 会话ID: {session_id} - 开始执行混合检索策略")
        
        # 1. 向量检索
        logger.info(f"📊 [步骤1/4] 会话ID: {session_id} - 执行向量检索")
        vector_results = self._vector_search(session_id, embedding_config, question)

        # 2. BM25 关键词检索
        logger.info(f"📊 [步骤2/4] 会话ID: {session_id} - 执行BM25关键词检索")
        bm25_results = self._bm25_search(session_id, question)

        # 3. RRF 融合
        logger.info(f"📊 [步骤3/4] 会话ID: {session_id} - 执行RRF融合算法")
        final_results = self._reciprocal_rank_fusion(vector_results, bm25_results)

        # 4. 取前 N 个结果
        logger.info(f"📊 [步骤4/4] 会话ID: {session_id} - 筛选最终结果")
        top_results = final_results[:settings.FINAL_CONTEXT_TOP_K]
        
        logger.info(f"✅ [混合检索完成] 会话ID: {session_id} - 最终返回 {len(top_results)} 个上下文块")
        
        # 记录最终结果的统计信息
        if top_results:
            total_chars = sum(len(chunk.content) for chunk in top_results)
            avg_score = sum(chunk.score for chunk in top_results) / len(top_results)
            logger.info(f"📈 [结果统计] 会话ID: {session_id} - 总字符数: {total_chars}, 平均分数: {avg_score:.4f}")
        
        return top_results

    def _vector_search(
            self,
            session_id: str,
            embedding_config: Dict[str, Any],
            question: str
    ) -> List[Tuple[str, float, Dict[str, Any]]]:
        """
        向量检索

        Args:
            session_id: 会话 ID
            embedding_config: Embedding 配置
            question: 用户问题

        Returns:
            List[Tuple[str, float, Dict[str, Any]]]: (文档ID, 分数, 元数据)
        """
        try:
            logger.info(f"🔍 [向量检索] 会话ID: {session_id} - 开始向量检索，问题长度: {len(question)} 字符")
            
            # 创建 embedding 配置对象
            # 确保 extra_params 不为 None
            embedding_config_copy = embedding_config.copy()
            if embedding_config_copy.get("extra_params") is None:
                embedding_config_copy["extra_params"] = {}
            
            embedding_cfg = EmbeddingConfig.from_dict(embedding_config_copy)
            logger.debug(f"🤖 [模型配置] 会话ID: {session_id} - 使用 {embedding_cfg.provider}/{embedding_cfg.model_name} 模型")

            # 加载 embedding 模型
            logger.debug(f"⚡ [模型加载] 会话ID: {session_id} - 正在加载 Embedding 模型...")
            embedding_model = EmbeddingManager.get_embedding_model(embedding_cfg)
            logger.debug(f"✅ [模型就绪] 会话ID: {session_id} - Embedding 模型加载完成")

            # 向量化问题
            logger.debug(f"🧠 [问题向量化] 会话ID: {session_id} - 正在将问题转换为向量...")
            question_embedding = embedding_model.embed_query(question)
            logger.debug(f"✅ [向量生成] 会话ID: {session_id} - 问题向量化完成，维度: {len(question_embedding)}")

            # 在向量数据库中搜索
            logger.debug(f"🔎 [数据库检索] 会话ID: {session_id} - 正在向量数据库中搜索相似文档...")
            results = get_vector_store().query_collection(
                collection_name=session_id,
                query_embedding=question_embedding,
                n_results=settings.VECTOR_SEARCH_TOP_K
            )
            logger.debug(f"📊 [检索结果] 会话ID: {session_id} - 向量数据库返回结果")

            # 转换结果格式
            vector_results = []
            if results["ids"] and results["ids"][0]:
                logger.info(f"✅ [检索成功] 会话ID: {session_id} - 找到 {len(results['ids'][0])} 个相似文档")
                for i, doc_id in enumerate(results["ids"][0]):
                    distance = results["distances"][0][i]
                    # 将距离转换为相似度分数（距离越小，分数越高）
                    score = 1.0 / (1.0 + distance)
                    metadata = results["metadatas"][0][i]
                    vector_results.append((doc_id, score, metadata))
                    
                    if i < 3:  # 只记录前3个结果的详细信息
                        file_path = metadata.get('file_path', 'unknown')
                        logger.debug(f"📄 [相似文档] 排名{i+1}: {file_path}, 距离: {distance:.4f}, 分数: {score:.4f}")
            else:
                logger.warning(f"⚠️ [无结果] 会话ID: {session_id} - 向量检索未找到相似文档")

            logger.info(f"🎯 [向量检索完成] 会话ID: {session_id} - 返回 {len(vector_results)} 个结果")
            return vector_results

        except Exception as e:
            logger.error(f"❌ [向量检索失败] 会话ID: {session_id} - {str(e)}")
            return []

    def _improved_tokenize(self, text: str) -> List[str]:
        """
        改进的分词方法，能更好地处理文件名和中英文混合内容
        
        Args:
            text: 待分词的文本
            
        Returns:
            List[str]: 分词结果
        """
        import re
        
        # 转换为小写
        text = text.lower()
        
        # 提取文件名（包含扩展名的完整文件名）
        file_pattern = r'[a-zA-Z0-9_-]+\.[a-zA-Z0-9]+'
        file_matches = re.findall(file_pattern, text)
        
        # 提取路径分隔符分割的部分
        path_pattern = r'[a-zA-Z0-9_-]+(?:/[a-zA-Z0-9_-]+)*'
        path_matches = re.findall(path_pattern, text)
        
        # 基本分词（空格、标点符号分割）
        basic_tokens = re.findall(r'[a-zA-Z0-9_-]+|[\u4e00-\u9fff]+', text)
        
        # 合并所有token
        all_tokens = set()
        all_tokens.update(basic_tokens)
        all_tokens.update(file_matches)
        
        # 为文件名添加不带扩展名的版本
        for file_match in file_matches:
            name_without_ext = file_match.split('.')[0]
            all_tokens.add(name_without_ext)
            
        # 过滤空字符串和单字符
        tokens = [token for token in all_tokens if len(token) > 1]
        
        return tokens

    def _calculate_file_name_bonus(self, query_tokens: List[str], documents: List[Dict], doc_scores: List[float]) -> List[float]:
        """
        计算文件名匹配的额外加分
        
        Args:
            query_tokens: 查询词列表
            documents: 文档列表
            doc_scores: 原始BM25分数
            
        Returns:
            List[float]: 每个文档的加分
        """
        import re
        
        # 从查询中提取可能的文件名
        file_name_patterns = []
        for token in query_tokens:
            # 检查是否是文件名格式
            if '.' in token and re.match(r'^[a-zA-Z0-9_-]+\.[a-zA-Z0-9]+$', token):
                file_name_patterns.append(token)
                # 同时添加不带扩展名的版本
                name_without_ext = token.split('.')[0]
                file_name_patterns.append(name_without_ext)
        
        bonus_scores = [0.0] * len(documents)
        
        if not file_name_patterns:
            return bonus_scores
            
        # 为每个文档计算文件名匹配加分
        for i, doc in enumerate(documents):
            file_path = doc["metadata"].get("file_path", "")
            if not file_path:
                continue
                
            # 提取文件名
            file_name = file_path.split('/')[-1].split('\\')[-1].lower()
            
            # 检查文件名匹配
            for pattern in file_name_patterns:
                if pattern.lower() in file_name:
                    # 精确匹配给更高分数
                    if pattern.lower() == file_name or pattern.lower() == file_name.split('.')[0]:
                        bonus_scores[i] += 10.0  # 精确匹配高分
                    else:
                        bonus_scores[i] += 5.0   # 部分匹配中等分
                        
            # 检查路径匹配
            for pattern in file_name_patterns:
                if pattern.lower() in file_path.lower():
                    bonus_scores[i] += 2.0   # 路径匹配低分
                    
        return bonus_scores

    def _bm25_search(
            self,
            session_id: str,
            question: str
    ) -> List[Tuple[str, float, Dict[str, Any]]]:
        """
        BM25 关键词检索

        Args:
            session_id: 会话 ID
            question: 用户问题

        Returns:
            List[Tuple[str, float, Dict[str, Any]]]: (文档ID, 分数, 元数据)
        """
        try:
            logger.info(f"🔤 [BM25检索] 会话ID: {session_id} - 开始关键词检索")
            
            # 获取或构建 BM25 索引
            bm25_index = self._get_bm25_index(session_id)
            if not bm25_index:
                logger.warning(f"⚠️ [索引缺失] 会话ID: {session_id} - BM25索引不存在")
                return []

            # 改进的分词逻辑
            query_tokens = self._improved_tokenize(question)
            logger.debug(f"📝 [分词结果] 会话ID: {session_id} - 查询词: {query_tokens}")

            # BM25 搜索
            logger.debug(f"🔍 [BM25计算] 会话ID: {session_id} - 正在计算BM25分数...")
            doc_scores = bm25_index.get_scores(query_tokens)

            # 获取文档信息
            documents = self._documents_cache.get(session_id, [])
            logger.debug(f"📚 [文档缓存] 会话ID: {session_id} - 缓存中有 {len(documents)} 个文档")

            # 检查是否包含文件名查询，给予额外加分
            file_name_bonus = self._calculate_file_name_bonus(query_tokens, documents, doc_scores)
            
            # 应用文件名加分
            for i, bonus in enumerate(file_name_bonus):
                if bonus > 0:
                    doc_scores[i] += bonus
                    logger.debug(f"📁 [文件名加分] 文档{i}: +{bonus:.4f}")

            # 排序并取前 N 个
            scored_docs = [
                (documents[i]["id"], score, documents[i]["metadata"])
                for i, score in enumerate(doc_scores)
                if score > 0
            ]
            scored_docs.sort(key=lambda x: x[1], reverse=True)
            
            top_results = scored_docs[:settings.BM25_SEARCH_TOP_K]
            logger.info(f"✅ [BM25完成] 会话ID: {session_id} - 找到 {len([s for s in doc_scores if s > 0])} 个匹配文档，返回前 {len(top_results)} 个")
            
            # 记录前几个结果的详细信息
            for i, (doc_id, score, metadata) in enumerate(top_results[:3]):
                file_path = metadata.get('file_path', 'unknown')
                logger.debug(f"📄 [BM25结果] 排名{i+1}: {file_path}, BM25分数: {score:.4f}")

            return top_results

        except Exception as e:
            logger.error(f"❌ [BM25检索失败] 会话ID: {session_id} - {str(e)}")
            return []

    def _get_bm25_index(self, session_id: str):
        """
        获取或构建 BM25 索引

        Args:
            session_id: 会话 ID

        Returns:
            BM25Okapi 索引或 None
        """
        # 检查缓存
        if session_id in self._bm25_cache:
            return self._bm25_cache[session_id]

        try:
            # 获取所有文档
            documents = get_vector_store().get_all_documents_from_collection(session_id)
            if not documents:
                return None

            # 准备文档文本（改进的分词）
            doc_texts = []
            for doc in documents:
                # 使用元数据中的内容
                content = doc["metadata"].get("content", doc["content"])
                # 提取文件路径信息
                file_path = doc["metadata"].get("file_path", "")
                # 组合内容和文件路径进行分词
                combined_content = f"{content} {file_path}"
                doc_texts.append(self._improved_tokenize(combined_content))

            # 构建 BM25 索引
            bm25_index = BM25Okapi(doc_texts)

            # 缓存索引和文档
            self._bm25_cache[session_id] = bm25_index
            self._documents_cache[session_id] = documents

            logger.info(f"为会话 {session_id} 构建了 BM25 索引，包含 {len(documents)} 个文档")
            return bm25_index

        except Exception as e:
            logger.error(f"构建 BM25 索引失败: {str(e)}")
            return None

    def _reciprocal_rank_fusion(
            self,
            vector_results: List[Tuple[str, float, Dict[str, Any]]],
            bm25_results: List[Tuple[str, float, Dict[str, Any]]],
            k: int = 60
    ) -> List[RetrievedChunk]:
        """
        RRF (Reciprocal Rank Fusion) 算法融合两个检索结果

        Args:
            vector_results: 向量检索结果
            bm25_results: BM25 检索结果
            k: RRF 参数

        Returns:
            List[RetrievedChunk]: 融合后的结果
        """
        logger.info(f"🔀 [RRF融合] 开始融合检索结果 - 向量结果: {len(vector_results)} 个, BM25结果: {len(bm25_results)} 个")
        
        # 创建文档 ID 到信息的映射
        doc_info = {}

        # 处理向量检索结果
        logger.debug(f"📊 [处理向量结果] 正在处理 {len(vector_results)} 个向量检索结果...")
        for rank, (doc_id, score, metadata) in enumerate(vector_results):
            if doc_id not in doc_info:
                doc_info[doc_id] = {
                    "metadata": metadata,
                    "content": metadata.get("content", ""),
                    "vector_rank": rank + 1,
                    "bm25_rank": None,
                    "rrf_score": 0.0
                }
            rrf_contribution = 1.0 / (k + rank + 1)
            doc_info[doc_id]["rrf_score"] += rrf_contribution
            
            if rank < 3:  # 记录前3个的详细信息
                file_path = metadata.get('file_path', 'unknown')
                logger.debug(f"📄 [向量贡献] {file_path} - 排名: {rank+1}, RRF贡献: {rrf_contribution:.4f}")

        # 处理 BM25 检索结果
        logger.debug(f"📊 [处理BM25结果] 正在处理 {len(bm25_results)} 个BM25检索结果...")
        for rank, (doc_id, score, metadata) in enumerate(bm25_results):
            if doc_id not in doc_info:
                doc_info[doc_id] = {
                    "metadata": metadata,
                    "content": metadata.get("content", ""),
                    "vector_rank": None,
                    "bm25_rank": rank + 1,
                    "rrf_score": 0.0
                }
            else:
                doc_info[doc_id]["bm25_rank"] = rank + 1
            rrf_contribution = 1.0 / (k + rank + 1)
            doc_info[doc_id]["rrf_score"] += rrf_contribution
            
            if rank < 3:  # 记录前3个的详细信息
                file_path = metadata.get('file_path', 'unknown')
                logger.debug(f"📄 [BM25贡献] {file_path} - 排名: {rank+1}, RRF贡献: {rrf_contribution:.4f}")

        # 按 RRF 分数排序
        logger.debug(f"🔄 [RRF排序] 正在按RRF分数排序 {len(doc_info)} 个文档...")
        sorted_docs = sorted(
            doc_info.items(),
            key=lambda x: x[1]["rrf_score"],
            reverse=True
        )

        # 转换为 RetrievedChunk 格式
        retrieved_chunks = []
        for i, (doc_id, info) in enumerate(sorted_docs):
            chunk = RetrievedChunk(
                id=doc_id,
                content=info["content"],
                file_path=info["metadata"].get("file_path", ""),
                start_line=info["metadata"].get("start_line"),
                score=info["rrf_score"],
                metadata=info["metadata"]
            )
            retrieved_chunks.append(chunk)
            
            # 记录前几个最终结果的详细信息
            if i < 5:
                file_path = info["metadata"].get('file_path', 'unknown')
                vector_rank = info["vector_rank"] or "N/A"
                bm25_rank = info["bm25_rank"] or "N/A"
                logger.debug(f"🏆 [最终排名{i+1}] {file_path} - RRF分数: {info['rrf_score']:.4f}, 向量排名: {vector_rank}, BM25排名: {bm25_rank}")

        logger.info(f"✅ [RRF融合完成] 融合后共 {len(retrieved_chunks)} 个结果")
        return retrieved_chunks

    def _generate_answer(
            self,
            question: str,
            retrieved_chunks: List[RetrievedChunk],
            llm_config: LLMConfigSchema
    ) -> str:
        """
        使用 LLM 生成答案

        Args:
            question: 用户问题
            retrieved_chunks: 检索到的上下文
            llm_config: LLM 配置

        Returns:
            str: 生成的答案
        """
        try:
            logger.info(f"🤖 [LLM生成] 开始生成答案 - 模型: {llm_config.provider}/{llm_config.model_name}")
            logger.info(f"📝 [上下文准备] 使用 {len(retrieved_chunks)} 个文档块作为上下文")
            
            # 创建 LLM 配置对象
            logger.debug(f"⚙️ [LLM配置] 提供商: {llm_config.provider}, 模型: {llm_config.model_name}, 温度: {llm_config.temperature}, 最大令牌: {llm_config.max_tokens}")
            logger.info(f"🔍 [DEBUG] _generate_answer 中的 llm_config:")
            logger.info(f"🔍 [DEBUG] - llm_config.provider: {llm_config.provider} (type: {type(llm_config.provider)})")
            logger.info(f"🔍 [DEBUG] - hasattr(llm_config.provider, 'value'): {hasattr(llm_config.provider, 'value')}")
            
            provider_value = llm_config.provider.value if hasattr(llm_config.provider, 'value') else llm_config.provider
            logger.info(f"🔍 [DEBUG] - 最终使用的 provider 值: {provider_value} (type: {type(provider_value)})")
            
            # 处理 extra_params，确保它是一个字典
            extra_params = llm_config.extra_params or {}
            logger.info(f"🔍 [DEBUG] - extra_params: {extra_params} (type: {type(extra_params)})")
            
            llm_cfg = LLMConfig(
                provider=provider_value,
                model_name=llm_config.model_name,
                api_key=llm_config.api_key,
                api_base=llm_config.api_base,
                api_version=llm_config.api_version,
                deployment_name=llm_config.deployment_name,
                temperature=llm_config.temperature,
                max_tokens=llm_config.max_tokens,
                **extra_params
            )

            # 加载 LLM 模型
            logger.info(f"🔧 [模型加载] 正在加载LLM模型...")
            llm = LLMManager.get_llm(llm_cfg)
            logger.info(f"✅ [模型就绪] LLM模型加载完成")

            # 构建 prompt
            logger.info(f"📋 [构建Prompt] 正在构建上下文和提示词...")
            context = self._build_context(retrieved_chunks)
            prompt = self._build_prompt(question, context)
            
            # 计算上下文统计信息
            context_chars = len(context)
            prompt_chars = len(prompt)
            logger.info(f"📊 [Prompt统计] 上下文长度: {context_chars} 字符, 完整Prompt长度: {prompt_chars} 字符")

            # 生成答案
            logger.info(f"🚀 [开始生成] 正在调用LLM生成答案...")
            response = llm.invoke(prompt)
            logger.info(f"✅ [生成完成] LLM响应已接收")

            # 提取答案文本
            if hasattr(response, 'content'):
                answer = response.content
            else:
                answer = str(response)
            
            logger.info(f"📤 [答案输出] 生成答案长度: {len(answer)} 字符")
            return answer

        except Exception as e:
            logger.error(f"❌ [生成失败] LLM答案生成失败: {str(e)}")
            return f"生成答案失败: {str(e)}"

    def _build_context(self, retrieved_chunks: List[RetrievedChunk]) -> str:
        """
        构建上下文字符串

        Args:
            retrieved_chunks: 检索到的文档块

        Returns:
            str: 格式化的上下文
        """
        context_parts = []

        for i, chunk in enumerate(retrieved_chunks):
            context_part = f"[文档 {i+1}] 文件: {chunk.file_path}"
            if chunk.start_line:
                context_part += f" (行 {chunk.start_line})"
            context_part += f"\n{chunk.content}\n"
            context_parts.append(context_part)

        return "\n".join(context_parts)

    def _build_prompt(self, question: str, context: str) -> str:
        """
        构建 LLM prompt

        Args:
            question: 用户问题
            context: 上下文

        Returns:
            str: 完整的 prompt
        """
        prompt = f"""你是一个专业的代码分析助手，请根据提供的代码仓库内容回答用户问题。

上下文信息：
{context}

用户问题：{question}

请根据上述上下文信息回答问题。如果上下文中没有相关信息，请明确说明。回答时请：
1. 提供准确、具体的信息
2. 引用相关的文件名和行号
3. 解释代码的功能和逻辑
4. 如果涉及多个文件，请说明它们之间的关系

回答："""

        return prompt

    def _log_query(
            self,
            db: Session,
            request: QueryRequest,
            response: QueryResponse,
            retrieved_chunks: List[RetrievedChunk]
    ):
        """
        记录查询日志

        Args:
            db: 数据库会话
            request: 查询请求
            response: 查询响应
            retrieved_chunks: 检索结果
        """
        try:
            query_log = QueryLog(
                session_id=request.session_id,
                question=request.question,
                answer=response.answer,
                retrieved_chunks_count=len(retrieved_chunks),
                generation_mode=request.generation_mode,
                llm_config=request.llm_config.model_dump() if request.llm_config else None,
                retrieval_time=response.retrieval_time,
                generation_time=response.generation_time,
                total_time=response.total_time
            )

            db.add(query_log)
            db.commit()

        except Exception as e:
            logger.error(f"记录查询日志失败: {str(e)}")
            db.rollback()


# 全局服务实例
query_service = QueryService()