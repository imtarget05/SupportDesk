# ADR-001: Why Azure Container Apps over AKS for Production Compute

> Canonical pack: `docs/adr/` at repo root (shared TMA ADR pack, Phase 1A + 3A). Edit the pack first, then sync here. Project deltas go in an addendum, never silent edits.

## Status
Accepted

## Context
Khi thiết kế hạ tầng điện toán đám mây cho các ứng dụng backend và AI microservices (như SupportDesk, SmartDocument, CreditFlow), có hai lựa chọn chính trong hệ sinh thái Microsoft Azure:
1. **Azure Kubernetes Service (AKS)**: Quản lý cụm Kubernetes đầy đủ với worker nodes, virtual networks, ingress controllers, và control plane.
2. **Azure Container Apps (ACA)**: Nền tảng container phi máy chủ (Serverless Container Platform) xây dựng trên nền tảng Kubernetes (K3s/Envoy/KEDA/Dapr) được Microsoft quản trị hoàn toàn.

Quy mô hiện tại của hệ thống:
- Lưu lượng giao dịch doanh nghiệp (ITSM, RAG queries, credit decision reviews) ở mức hàng ngàn đến hàng chục ngàn requests/ngày, không phải hàng trăm triệu requests/giây.
- Đội ngũ kỹ thuật ưu tiên tập trung vào business logic, AI orchestration, và data integrity thay vì quản trị cụm Kubernetes (node upgrades, OS patching, CNI configuration, etcd backup, ingress controller tuning).

## Decision
Chúng tôi quyết định chọn **Azure Container Apps (ACA)** làm Compute Plane chính cho môi trường Production:
1. **Serverless Container Lifecycle**: ACA chạy trực tiếp các Docker container chuẩn mà không cần viết boilerplate YAML Kubernetes phức tạp cho từng môi trường cơ bản.
2. **KEDA Event-Driven Autoscaling & Scale-to-Zero**: Hỗ trợ tự động scale từ 0 lên N instances dựa trên HTTP traffic, Azure Service Bus queue depth, hoặc CPU/memory utilization (`min_replicas = 0`). Khi không có traffic, hệ thống tự động về 0 giúp tiết kiệm 100% chi phí compute.
3. **Built-in Ingress & Traffic Splitting**: Tích hợp sẵn Envoy-based ingress, hỗ trợ HTTPS tự động, custom domains, internal service discovery, và Blue/Green / Canary traffic splitting giữa các Revisions mà không cần cài đặt thêm Nginx Ingress hay cert-manager.
4. **Vị thế của AKS**: Cụm manifests Kubernetes (`k8s/`) và nền tảng `AKS-SRE-Platform` (kết hợp ArgoCD GitOps, KEDA, Envoy Gateway) vẫn được duy trì như một **Advanced SRE / Portability Target**. Khi hệ thống mở rộng quy mô cần custom kernel modules, specialized networking CNI, hoặc multi-tenant hard isolation, toàn bộ workload có thể chuyển đổi sang AKS trong vài giờ mà không phải sửa code.

## Consequences
### Positive
- Giảm thiểu 80% gánh nặng vận hành hạ tầng (operational overhead) so với việc duy trì cluster AKS riêng biệt.
- Tận dụng tối đa mô hình thanh toán per-second khi container thực thi.
- Giữ vững tính đóng gói container chuẩn Docker: có thể chạy ở local (Docker Compose), Render (Preview), Azure Container Apps (Prod), hoặc AKS (Enterprise).

### Negative / Trade-offs
- Không can thiệp sâu vào tầng Kubernetes control plane primitives (DaemonSets, custom admission controllers).
- Khi scale-to-zero, request đầu tiên sau thời gian rảnh sẽ có cold-start (khoảng 2–4 giây), có thể khắc phục bằng cách cấu hình `min_replicas = 1` trong giờ hành chính.
