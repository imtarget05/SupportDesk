# ADR-005: Positioning Render as an Ephemeral Preview & Recruiter Demo Plane

## Status
Accepted

## Context
Trong việc xây dựng Portfolio kỹ thuật phục vụ tuyển dụng, một yêu cầu quan trọng là:
- Nhà tuyển dụng hoặc người xem CV có thể mở link và tương tác trực tiếp với ứng dụng thật trên web (live running demo).
- Không thể yêu cầu ứng viên phải chi trả hàng chục hoặc hàng trăm USD/tháng để duy trì cụm server Azure/AWS enterprise chạy 24/7 chỉ để chờ nhà tuyển dụng click xem.
- Tuy nhiên, nhiều portfolio mắc sai lầm nghiêm trọng khi sử dụng các gói miễn phí (như Render Free Tier, Heroku Free cũ) rồi tuyên bố hệ thống đạt "High Availability 99.9%", "Production Ready", hoặc "Enterprise Resilient".

Đặc tính kỹ thuật thực tế của Render Free Tier:
- Web service tự động "sleep" (spin down) sau 15 phút không có request; request tiếp theo mất 30–50 giây để khởi động lại (cold-start).
- File system mang tính tạm thời (ephemeral), dữ liệu cục bộ bị xóa sau khi service khởi động lại.
- Giới hạn 1 instance duy nhất, không hỗ trợ scale nhiều instances trên free plan.
- Render Background Workers là dịch vụ trả phí ($7/tháng), không nằm trong danh mục free tier.

## Decision
Chúng tôi quyết định định vị **Render** chính xác theo vai trò kỹ thuật thực tế của nó:
1. **Render = Preview / Demo / Sandbox Plane (PROFILE=portfolio)**:
   - Dùng Render để host live demo miễn phí cho nhà tuyển dụng xem thử nghiệm UI, test API cơ bản và thẩm định mã nguồn.
   - Gắn nhãn trung thực trong README và CV: Đây là **Portfolio Demo Environment**, không tự nhận là High-Availability Enterprise Production.
2. **Xử lý Worker trên Render Free mà không tốn chi phí**:
   - Đối với SupportDesk, cơ chế outbox publisher và event consumer được tích hợp tùy chọn chạy dạng in-process background worker trong lifespan của FastAPI service (`START_BACKGROUND_WORKERS=true`), giúp toàn bộ luồng sự kiện vẫn hoạt động trơn tru trong 1 container miễn phí duy nhất.
3. **Môi trường Production thực sự thuộc về Azure (PROFILE=production)**:
   - Nơi có Azure Container Apps, Azure Service Bus, PostgreSQL Flexible Server, Managed Identity và Private Endpoints. Được định nghĩa chi tiết bằng mã nguồn IaC (Terraform / Bicep) để chứng minh năng lực thiết kế enterprise production.

## Consequences
### Positive
- Chi phí vận hành portfolio thực tế là **$0/tháng** mà vẫn có link chạy thật để gửi CV.
- Thể hiện sự trung thực và hiểu biết sâu sắc về hạ tầng: phân biệt rạch ròi giữa môi trường demo tiết kiệm chi phí và kiến trúc production chịu tải thực tế.

### Negative / Trade-offs
- Nhà tuyển dụng truy cập lần đầu có thể gặp độ trễ cold-start 30 giây khi Render đánh thức container.
