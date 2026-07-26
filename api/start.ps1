# تشغيل Fomo Family API
# Run from the api/ directory

Write-Host "=== Fomo Family API ===" -ForegroundColor Cyan
Write-Host ""
Write-Host "Choose an option:" -ForegroundColor Yellow
Write-Host "  1) Docker (includes Redis automatically)"
Write-Host "  2) Local (Python only, dev mode without Redis)"
Write-Host "  3) Local (Python + Redis)"
Write-Host "  4) Run tests only"
Write-Host ""
$choice = Read-Host "Enter choice [1-4]"

switch ($choice) {
    "1" {
        Write-Host "Starting with Docker Compose..." -ForegroundColor Green
        docker compose up --build
    }
    "2" {
        Write-Host "Starting in dev mode (no Redis needed)..." -ForegroundColor Green
        $env:FOMO_API_DEV = "true"
        uvicorn fomo_api.main:app --reload --port 8000
    }
    "3" {
        Write-Host "Starting with local Redis..." -ForegroundColor Green
        Write-Host "Make sure Redis is running on localhost:6379" -ForegroundColor Yellow
        uvicorn fomo_api.main:app --reload --port 8000
    }
    "4" {
        Write-Host "Running tests..." -ForegroundColor Green
        py -m pytest tests/ -v
    }
    default {
        Write-Host "Invalid choice" -ForegroundColor Red
    }
}
