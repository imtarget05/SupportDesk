# ADR-003: Dual Vector Retrieval Strategy — Azure AI Search vs Qdrant

## Status
Accepted

## Context
Trong hệ thống SmartDocument (Enterprise Knowledge Retrieval System), việc truy xuất tài liệu pháp lý, quy chế và tài liệu kỹ thuật đòi hỏi:
- Khả năng tìm kiếm ngữ nghĩa (Semantic Vector Search) qua dense embeddings.
- Khả năng tìm kiếm từ khóa chính xác (Lexical BM25 Search) cho các điều luật (ví dụ: "Điều 32 khoản 1"), mã hợp đồng, số hiệu công văn, tên riêng.
- Hợp nhất kết quả đa phương thức (Hybrid Search with Reciprocal Rank Fusion - RRF).

Hai công nghệ tìm kiếm được xem xét:
1. **Qdrant**: Vector database chuyên dụng mã nguồn mở, hỗ trợ cosine, dot product, payload filtering, và HNSW indexing. Rất linh hoạt, chạy được cục bộ (embedded / Docker) và có Qdrant Cloud.
2. **Azure AI Search**: Dịch vụ tìm kiếm cấp doanh nghiệp của Microsoft, hỗ trợ Native Hybrid Search (BM25 + Vector Search) tích hợp sẵn RRF, semantic re-ranking (deep learning ranker), và bảo mật cấp enterprise.

## Decision
Chúng tôi quyết định thiết kế mô hình **Clean Architecture Retrieval Port (`RetrievalProvider`)** hỗ trợ 2 Adapter độc lập:
1. **`QdrantRetriever` (Local & Portfolio Profile)**:
   - Dùng cho môi trường Local Development và Profile `portfolio` (kết nối Qdrant Cloud Free Tier).
   - Cho phép chạy hoàn toàn độc lập mà không cần tài khoản Azure hay chi phí duy trì.
2. **`AzureAISearchRetriever` (Enterprise Production Profile)**:
   - Dùng cho môi trường Production của khách hàng doanh nghiệp trên Azure.
   - Tận dụng cơ chế tìm kiếm Hybrid Search nguyên bản: chạy đồng thời full-text search (BM25) và vector search trên cùng một chỉ mục, sau đó tự động dung hòa qua thuật toán RRF.
   - Tích hợp trực tiếp với Azure Managed Identity, không cần quản lý API key tĩnh trong cấu hình.

## Consequences
### Positive
- Ứng dụng không bị khóa chặt (no vendor lock-in) vào một nhà cung cấp cơ sở dữ liệu vector duy nhất.
- Tại phỏng vấn, chứng minh được tư duy kiến trúc: *"Application phân tách ranh giới rõ ràng: RAG orchestration nằm ở tầng Application, còn Vector/Keyword retrieval là Port có thể cắm Qdrant hoặc Azure AI Search tùy theo quy mô và ràng buộc chi phí."*

### Negative / Trade-offs
- Cần duy trì 2 lớp mapping dữ liệu (Qdrant PointStruct vs Azure AI Search Document Index Schema).
