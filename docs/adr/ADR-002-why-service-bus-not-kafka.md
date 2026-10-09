# ADR-002: Why Azure Service Bus over Apache Kafka for ITSM Domain Messaging

> Canonical pack: `docs/adr/` at repo root (shared TMA ADR pack, Phase 1A + 3A). Edit the pack first, then sync here. Project deltas go in an addendum, never silent edits.

## Status
Accepted

## Context
Trong hệ thống SupportDesk, các hoạt động nghiệp vụ cốt lõi bao gồm:
- `TicketCreated`: Khởi tạo yêu cầu hỗ trợ, kích hoạt tính toán hạn chót SLA.
- `TicketAssigned`: Gán kỹ thuật viên chịu trách nhiệm, gửi email/thông báo.
- `TicketStatusChanged`: Chuyển đổi trạng thái theo quy trình máy trạng thái có kiểm soát.
- `SLAWarningTriggered`: Cảnh báo khi thời gian xử lý đạt 80% ngưỡng cho phép.
- `TicketResolved`: Kết thúc sự cố, cập nhật audit log và chỉ số SLA tuân thủ.

Hai công nghệ truyền thông điệp chính được cân nhắc:
1. **Apache Kafka / Azure Event Hubs**: Nền tảng phân tán append-only distributed log, tối ưu cho luồng dữ liệu thông lượng cực lớn (telemetry, clickstream, IoT events hàng triệu tin nhắn/giây), phân vùng partition cố định, quản lý offset do consumer kiểm soát.
2. **Azure Service Bus**: Enterprise Message Broker cao cấp được quản trị hoàn toàn, tối ưu cho các giao dịch nghiệp vụ (business transaction messaging) với cơ chế Queues và Topics/Subscriptions.

## Decision
Chúng tôi quyết định chọn **Azure Service Bus** làm message broker chuẩn cho môi trường Production của SupportDesk:
1. **Đúng bản chất nghiệp vụ (Right Tool for the Job)**:
   - SupportDesk là bài toán **Enterprise Workflow & Transactional Messaging**, không phải bài toán telemetry streaming.
   - Số lượng ticket một ngày từ vài trăm đến hàng chục ngàn; mỗi thông điệp là một lệnh nghiệp vụ quan trọng đòi hỏi xử lý tin cậy tuyệt đối (reliable delivery).
2. **Tính năng gốc vượt trội cho nghiệp vụ**:
   - **Duplicate Detection (At-most-once deduplication)**: Tự động loại bỏ tin nhắn trùng lặp dựa trên `MessageId` trong cửa sổ thời gian xác định mà không cần triển khai thêm tầng cache ngoài.
   - **Dead Letter Queue (DLQ)**: Cô lập tự động các tin nhắn lỗi quá số lần retry mà không làm tắc nghẽn hàng đợi chính.
   - **Scheduled Messages**: Hỗ trợ hẹn giờ gửi tin nhắn (ví dụ: kích hoạt cảnh báo SLA sau 3.2 giờ tương ứng 80% SLA) mà không cần duy trì scheduler phức tạp trong ứng dụng.
   - **Sessions & Message Ordering**: Đảm bảo thứ tự tuyệt đối theo từng `TicketId` (Message Session) giữa các consumers phân tán.
3. **Vị thế của Apache Kafka**:
   - Vẫn cung cấp Kafka Adapter (`KafkaEventProducer` / `aiokafka`) và Kafka KRaft broker trong Docker Compose / Kubernetes để phục vụ kiểm thử cục bộ và kịch bản kết nối hệ thống viễn thông / telemetry thông lượng cao.
   - Trong phỏng vấn kiến trúc, sự khác biệt này chứng minh năng lực hiểu sâu về bản chất hệ phân tán: *"Tôi không chọn Kafka theo trào lưu; SupportDesk là transactional business messaging nên Service Bus là lựa chọn chính xác. Với bài toán streaming telemetry, tôi sẽ chọn Kafka/Event Hubs."*

## Consequences
### Positive
- Loại bỏ hoàn toàn chi phí và rủi ro tự vận hành Kafka cluster (Zookeeper, KRaft quorum, rebalancing lag, storage retention).
- Tích hợp tự nhiên với Azure Managed Identity (kết nối không cần password/connection string cứng).
- Consumer scaling tự động qua KEDA dựa trên độ sâu hàng đợi Service Bus.

### Negative / Trade-offs
- Giới hạn kích thước thông điệp tối đa 1MB (đối với Tier Standard/Premium), đòi hỏi các file đính kèm lớn phải lưu tại Azure Blob Storage và chỉ truyền URI qua message payload.
