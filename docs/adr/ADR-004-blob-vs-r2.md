# ADR-004: Enterprise Data Sovereignty & Object Storage — Azure Blob Storage vs Cloudflare R2

## Status
Accepted

## Context
Các ứng dụng trong hệ thống cần lưu trữ các đối tượng phi cấu trúc (unstructured objects):
1. **SmartDocument**: File tài liệu PDF, DOCX, scan của hợp đồng nội bộ và chính sách pháp lý.
2. **CreditFlow**: Hồ sơ tài chính, báo cáo sao kê ngân hàng, hợp đồng tín dụng và model bundles (`pipeline.joblib`).
3. **Public Assets**: Static build files của React frontend, avatar công khai, tài liệu mẫu demo.

Hai dịch vụ Object Storage được xem xét:
1. **Cloudflare R2**: Lưu trữ đối tượng tương thích S3, ưu điểm nổi bật là **$0 egress fees** (không tính phí tải dữ liệu ra ngoài) và tích hợp liền mạch với Cloudflare CDN Edge.
2. **Azure Blob Storage**: Dịch vụ lưu trữ đối tượng cấp doanh nghiệp trên Azure, hỗ trợ Private Endpoints (VNet integration), mã hóa với Customer-Managed Keys (Azure Key Vault), và bảo mật định danh qua Microsoft Entra ID.

## Decision
Chúng tôi quyết định phân loại dữ liệu và áp dụng chính sách lưu trữ phân tầng:
1. **Dữ liệu nhạy cảm / Nội bộ doanh nghiệp / Mô hình ML → Azure Blob Storage**:
   - Tất cả tài liệu hợp đồng nội bộ của SmartDocument, toàn bộ hồ sơ khách hàng của CreditFlow, và các artifacts ML phải lưu trong Azure Blob Storage.
   - Lý do: Tuân thủ yêu cầu về **Data Residency**, cô lập mạng hoàn toàn qua Private Endpoint (không cho phép truy cập qua public internet), và xác thực bảo mật thông qua Azure Managed Identity thay vì phát tán Access Keys.
   - Tránh mô hình rủi ro: API trong Azure phải gọi ra ngoài internet sang Cloudflare R2 chỉ để đọc hồ sơ tín dụng mật.
2. **Tài liệu công khai / Demo / Frontend Assets → Cloudflare R2 & CDN**:
   - Sử dụng Cloudflare R2 cho tài liệu mẫu trong môi trường demo/portfolio, tài liệu public, và các bản xuất bản báo cáo công khai để tận dụng tối đa lợi thế $0 chi phí băng thông (zero egress fees).

## Consequences
### Positive
- Đáp ứng các tiêu chuẩn khắt khe về an toàn thông tin tài chính (Banking Secrecy, GDPR, SBV guidelines).
- Tối ưu hóa chi phí: Dữ liệu công khai có lưu lượng tải lớn không làm phát sinh chi phí egress khổng lồ của Azure; dữ liệu bảo mật được bảo vệ toàn diện trong mạng nội bộ Azure.
