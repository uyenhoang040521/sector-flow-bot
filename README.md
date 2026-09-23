# Sector Flow Bot 📊

Mỗi ngày lúc 07:00 (giờ VN), bot tổng hợp dòng tiền crypto theo sector và ghi báo cáo vào `reports/latest.md`.

## Bot đo gì

| Chỉ số | Nguồn | Ý nghĩa |
|---|---|---|
| Market cap thay đổi 24h (% và $) | CoinGecko `/coins/categories` | Sector nào đang được định giá lên mạnh nhất |
| Đột biến volume | CoinGecko + lịch sử bot tự lưu | Volume hôm nay so với trung bình 7 ngày (≥ 1.5x là đột biến) |
| Dòng stablecoin theo chain | DefiLlama `stablecoins.llama.fi` | Chain nào đang được bơm / rút USD stablecoin (24h và 7 ngày) |
| Tín hiệu hội tụ | Kết hợp | Ecosystem vừa tăng vốn hóa, vừa có stablecoin chảy vào chain tương ứng |

Không cần API key. CoinMarketCap chưa dùng (có thể thêm sau).

## Cài đặt (5 phút)

1. Tạo repo mới trên GitHub (public hoặc private), upload toàn bộ thư mục này (giữ nguyên thư mục `.github/workflows`).
2. Vào **Settings → Actions → General → Workflow permissions**, chọn **Read and write permissions** → Save.
3. Vào tab **Actions → Daily sector flow report → Run workflow** để chạy thử lần đầu.
4. Xem kết quả ở `reports/latest.md` hoặc trong phần Summary của lần chạy.

**Tùy chọn:** đăng ký [CoinGecko Demo API key](https://www.coingecko.com/en/api) miễn phí, thêm vào **Settings → Secrets and variables → Actions** với tên `COINGECKO_API_KEY` để tránh bị giới hạn tần suất.

## Chỉnh cấu hình

Trong `flowbot.py`, mục `CONFIG`:

- `min_category_mcap` — bỏ sector có vốn hóa dưới $100M (lọc nhiễu)
- `min_category_volume` — bỏ sector có volume dưới $5M
- `min_chain_stable` — bỏ chain có dưới $10M stablecoin
- `exclude_keywords` — loại các category kiểu "portfolio", "launchpool"...
- `top_n` — số dòng mỗi bảng
- `volume_spike_min_ratio` — ngưỡng đột biến volume

Đổi giờ chạy: sửa dòng `cron` trong `.github/workflows/daily-report.yml` (giờ UTC; VN = UTC+7).

## Lưu ý

- Mục đột biến volume cần ≥ 3 ngày lịch sử, nên sẽ xuất hiện từ lần chạy thứ 4.
- Thay đổi market cap bao gồm cả biến động giá, không phải 100% là tiền mới vào.
- Category của CoinGecko chồng lấn nhau (1 coin có thể thuộc nhiều sector).
- GitHub có thể tạm dừng workflow theo lịch nếu repo không có hoạt động trong 60 ngày; bot tự commit mỗi ngày nên thường không bị ảnh hưởng.
