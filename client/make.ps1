<#
.SYNOPSIS
    PowerShell аналог Makefile для сборки и публикации Docker образа.

.DESCRIPTION
    Использование:
    .\make.ps1 [цель] [-AwsAccountId <id>] [-AwsRegion <region>]
#>

param (
    [Parameter(Position = 0)]
    [string]$Target = 'help',

    [string]$AwsAccountId = '123456789012',
    [string]$AwsRegion = 'eu-west-1'
)

$ErrorActionPreference = 'Stop'

# Определение переменных
$RepoUrl = "$AwsAccountId.dkr.ecr.$AwsRegion.amazonaws.com/upg"
$ImageName = "client"

# Получаем короткий хэш коммита (отрезаем пробелы/переносы строк)
try {
    $GitCommit = (git rev-parse --short HEAD).Trim()
} catch {
    Write-Warning "Не удалось получить git commit. Убедитесь, что вы находитесь в git-репозитории."
    $GitCommit = "unknown"
}

$Tag = "sha-$GitCommit"
$FullImageName = "$RepoUrl/${ImageName}:$Tag"

# --- ФУНКЦИИ (Аналоги целей Makefile) ---

function Invoke-Help {
    Write-Host "Доступные команды:" -ForegroundColor Yellow
    $helpText = [ordered]@{
        "help"         = "Показать эту справку"
        "install"      = "Установить зависимости npm"
        "login"        = "Авторизоваться в AWS ECR"
        "build-image"  = "Собрать Docker образ клиента"
        "push-image"   = "Отправить Docker образ клиента в ECR"
        "docker-build" = "Авторизация + Сборка"
        "docker-push"  = "Авторизация + Сборка + Отправка"
        "docker-run"   = "Запустить контейнер локально"
        "docker-shell" = "Открыть оболочку (/bin/sh) внутри контейнера"
        "clean-local"  = "Удалить локальные Docker образы"
    }

    foreach ($key in $helpText.Keys) {
        Write-Host ("{0,-20} {1}" -f $key, $helpText[$key]) -ForegroundColor Cyan
    }
}

function Invoke-Install {
    Write-Host "🚀 Installing dependencies" -ForegroundColor Green
    npm install
}

function Invoke-Login {
    Write-Host "🚀 Login into ECR" -ForegroundColor Green
    # Получаем пароль и передаем его через пайплайн в docker login
    aws ecr get-login-password --region $AwsRegion | docker login --username AWS --password-stdin "$AwsAccountId.dkr.ecr.$AwsRegion.amazonaws.com"
}

function Invoke-BuildImage {
    Write-Host "🚀 Building client image: $FullImageName" -ForegroundColor Green
    $env:DOCKER_BUILDKIT = "1"
    docker buildx build --platform=linux/amd64 -t $FullImageName ./
}

function Invoke-PushImage {
    Write-Host "🚀 Pushing: $FullImageName" -ForegroundColor Green
    docker push $FullImageName
}

# --- МАРШРУТИЗАТОР (Выполнение целей) ---

switch ($Target.ToLower()) {
    'help' {
        Invoke-Help
    }
    'install' {
        Invoke-Install
    }
    'login' {
        Invoke-Login
    }
    'build-image' {
        Invoke-BuildImage
    }
    'push-image' {
        Invoke-PushImage
    }
    'docker-build' {
        Invoke-Login
        Invoke-BuildImage
    }
    'docker-push' {
        Invoke-Login
        Invoke-BuildImage
        Invoke-PushImage
    }
    'docker-run' {
        Write-Host "🚀 Running client image: $FullImageName" -ForegroundColor Green
        docker run -p 3000:3000 --rm $FullImageName
    }
    'docker-shell' {
        docker run -it --rm $FullImageName /bin/sh
    }
    'clean-local' {
        Write-Host "Удаление $FullImageName" -ForegroundColor Yellow
        # 2>$null подавляет вывод ошибок, аналог '|| true' в bash
        docker rmi $FullImageName 2>$null
    }
    default {
        Write-Host "Неизвестная команда: $Target" -ForegroundColor Red
        Invoke-Help
    }
}
