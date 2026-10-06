[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$Url,

    [string]$OutputDirectory,

    [switch]$CatalogOnly,

    [switch]$KeepVideos,

    [ValidateRange(1, 10000)]
    [int]$MaxVideos = 10000,

    [ValidateRange(1, 1000)]
    [int]$MaxPages = 1000
)

$ErrorActionPreference = 'Stop'
$UserAgent = 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 Mobile/15E148 Safari/604.1'
$JingxuanHost = 'jingxuan.douyin.com'
$MediaHostSuffixes = @('douyinvod.com', 'douyinstatic.com', 'douyin.com', 'ixiguavideo.com', 'bdxiguastatic.com', 'zjcdn.com', 'bytecdn.cn', 'ibytedtos.com')

function Get-InputUri {
    param([string]$Text)
    $found = [regex]::Match($Text, 'https://[^\s<>"'']+')
    if (-not $found.Success) { throw '没有找到 HTTPS 抖音账号链接。' }
    $candidate = $found.Value.TrimEnd([char[]]@(')', ']', '}', '。', '，', ',', ';', '；', '!', '！'))
    $uri = $null
    if (-not [Uri]::TryCreate($candidate, [UriKind]::Absolute, [ref]$uri)) { throw '账号链接格式无效。' }
    if ($uri.Scheme -ne 'https') { throw '只接受 HTTPS 链接。' }
    if ($uri.UserInfo -or -not $uri.IsDefaultPort) { throw '账号链接不能包含登录凭证或非默认端口。' }
    $allowed = @('v.douyin.com', 'jingxuan.douyin.com', 'www.douyin.com', 'www.iesdouyin.com')
    if ($uri.Host.ToLowerInvariant() -notin $allowed) { throw '仅支持抖音账号主页分享链接。' }
    return $uri
}

function Get-FinalUri {
    param($Response)
    $base = $Response.BaseResponse
    if ($base.PSObject.Properties['ResponseUri'] -and $base.ResponseUri) { return $base.ResponseUri }
    if ($base.PSObject.Properties['RequestMessage'] -and $base.RequestMessage) { return $base.RequestMessage.RequestUri }
    throw '无法确认账号分享链接的最终地址。'
}

function Get-ResponseText {
    param($Response)
    # Windows PowerShell 5.1 may decode JSON without a charset as Latin-1.
    return [System.Text.Encoding]::UTF8.GetString($Response.RawContentStream.ToArray())
}

function Resolve-AccountId {
    param([Uri]$InputUri)
    if ($InputUri.Host -eq 'jingxuan.douyin.com' -and $InputUri.AbsolutePath -match '^/m/user/(\d{15,20})/?$') {
        return $Matches[1]
    }
    if ($InputUri.AbsolutePath -match '^/user/(\d{15,20})/?$') { return $Matches[1] }
    if ($InputUri.AbsolutePath -match '^/(?:video|note)/' -or $InputUri.AbsolutePath -match '^/m/video/') {
        throw '这是单条作品链接；请复制账号主页的“分享主页”短链。'
    }
    if ($InputUri.Host -eq 'www.douyin.com' -and $InputUri.AbsolutePath -match '^/user/MS4') {
        throw '普通 secUID 长主页链接无法稳定匿名转换；请在抖音账号主页点击“分享”，复制 v.douyin.com 短链。'
    }
    $response = Invoke-WebRequest -Uri $InputUri.AbsoluteUri -UseBasicParsing -MaximumRedirection 5 -TimeoutSec 30 -Headers @{ 'User-Agent' = $UserAgent }
    $finalUri = Get-FinalUri $response
    if ($finalUri.Host.ToLowerInvariant() -notin @('www.iesdouyin.com', 'www.douyin.com', 'v.douyin.com')) {
        throw '分享短链跳转到了非抖音域名，已停止。'
    }
    if ($finalUri.AbsolutePath -notmatch '^/share/user/' -and $finalUri.AbsolutePath -notmatch '^/user/') {
        throw '分享短链不是账号主页；请复制作者主页的分享短链。'
    }
    $decoded = [Uri]::UnescapeDataString($finalUri.AbsoluteUri)
    $match = [regex]::Match($decoded, '"social_author_id"\s*:\s*"(?<id>\d{15,20})"')
    if (-not $match.Success) {
        throw '分享链接未公开数字作者 ID；请重新复制账号主页的分享短链。'
    }
    return $match.Groups['id'].Value
}

function Get-AccountInfo {
    param([string]$AccountId)
    $uri = "https://$JingxuanHost/m/user/$AccountId"
    $response = Invoke-WebRequest -Uri $uri -UseBasicParsing -TimeoutSec 30 -Headers @{ 'User-Agent' = $UserAgent }
    $html = Get-ResponseText $response
    $marker = 'window._SSR_DATA = '
    $start = $html.IndexOf($marker)
    if ($start -lt 0) { throw '公开作者页没有返回可识别的资料。' }
    $end = $html.IndexOf('</script>', $start)
    if ($end -lt 0) { throw '公开作者页资料不完整。' }
    $raw = $html.Substring($start + $marker.Length, $end - $start - $marker.Length).Trim().TrimEnd([char[]]@(';'))
    $state = $raw | ConvertFrom-Json
    $info = $state.data.storeState.'user-page'.userInfo.result
    if (-not $info -or [string]::IsNullOrWhiteSpace([string]$info.name)) {
        throw '该作者在公开精选页不可访问；没有使用账号或 Cookie 重试。'
    }
    return [pscustomobject]@{ Name = [string]$info.name; ExpectedCount = [int]$info.videoTotalCount }
}

function Get-PublicVideos {
    param([string]$AccountId, [int]$PageLimit)
    $seen = New-Object 'System.Collections.Generic.HashSet[string]'
    $videos = New-Object 'System.Collections.Generic.List[object]'
    $cursor = '0'
    $complete = $false
    $pages = 0
    for ($page = 1; $page -le $PageLimit; $page++) {
        $uri = "https://$JingxuanHost/yumme/v1/web/filter/post/?aid=392139&app_name=yumme&version_code=26.1.0&device_platform=web&pull_type=2&filter_type=2&max_cursor=$cursor&min_cursor=0&count=10&user_id=$AccountId"
        $response = Invoke-WebRequest -Uri $uri -UseBasicParsing -TimeoutSec 30 -Headers @{ 'User-Agent' = $UserAgent; Referer = "https://$JingxuanHost/m/user/$AccountId" }
        $body = (Get-ResponseText $response) | ConvertFrom-Json
        $pages++
        if ([int]$body.status_code -ne 0) { throw "公开作品列表返回状态 $($body.status_code)，已停止。" }
        $items = if ($null -eq $body.items) { @() } else { @($body.items) }
        if ($items.Count -eq 0 -and [int]$body.has_more -eq 1) { throw '作品列表还有下一页却返回空页，已停止。' }
        foreach ($item in $items) {
            $id = [string]$item.aweme_id
            if ($id -notmatch '^\d{15,20}$' -or -not $seen.Add($id)) { continue }
            $media = $null
            if ($item.video -and $item.video.play_addr_h264 -and @($item.video.play_addr_h264.url_list).Count -gt 0) {
                $media = $item.video.play_addr_h264
            } elseif ($item.video -and $item.video.play_addr -and @($item.video.play_addr.url_list).Count -gt 0) {
                $media = $item.video.play_addr
            }
            $urls = @()
            $bytes = 0L
            if ($media) {
                $urls = @($media.url_list | Where-Object { -not [string]::IsNullOrWhiteSpace([string]$_) })
                if ($media.data_size) { $bytes = [long]$media.data_size }
            }
            $videos.Add([pscustomobject]@{
                Id = $id
                Title = [string]$item.desc
                CreatedAt = [long]$item.create_time
                DurationMs = [long]$item.video.duration
                ExpectedBytes = $bytes
                MediaUrls = $urls
            })
        }
        if ([int]$body.has_more -ne 1) { $complete = $true; break }
        $next = [string]$body.max_cursor
        if ([string]::IsNullOrWhiteSpace($next) -or $next -eq $cursor) { throw '公开作品列表分页游标未前进，已停止。' }
        $cursor = $next
        Start-Sleep -Milliseconds 700
    }
    return [pscustomobject]@{ Videos = $videos.ToArray(); Pages = $pages; EndReached = $complete }
}

function Get-SafeTitle {
    param([string]$Title)
    $safe = $Title -replace '[<>:"/\\|?*\x00-\x1F]', ' '
    $safe = ($safe -replace '\s+', ' ').Trim().TrimEnd([char[]]@('.', ' '))
    if ([string]::IsNullOrWhiteSpace($safe)) { return '未命名作品' }
    if ($safe.Length -gt 60) { return $safe.Substring(0, 60).Trim().TrimEnd([char[]]@('.', ' ')) }
    return $safe
}

function Test-TrustedMediaUri {
    param([string]$Value)
    $uri = $null
    if (-not [Uri]::TryCreate($Value, [UriKind]::Absolute, [ref]$uri)) { return $false }
    if ($uri.Scheme -ne 'https') { return $false }
    $hostName = $uri.Host.ToLowerInvariant()
    foreach ($suffix in $MediaHostSuffixes) {
        if ($hostName -eq $suffix -or $hostName.EndsWith('.' + $suffix)) { return $true }
    }
    return $false
}

function Test-ValidMp4 {
    param([string]$Path, [long]$ExpectedBytes)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $false }
    $file = Get-Item -LiteralPath $Path
    if ($file.Length -lt 32) { return $false }
    if ($ExpectedBytes -gt 0 -and $file.Length -ne $ExpectedBytes) { return $false }
    $stream = [System.IO.File]::OpenRead($Path)
    try {
        $bytes = New-Object byte[] 12
        if ($stream.Read($bytes, 0, 12) -ne 12) { return $false }
        $brand = [System.Text.Encoding]::ASCII.GetString($bytes, 4, 4)
        return ($brand -eq 'ftyp' -or $brand -eq 'styp')
    } finally {
        $stream.Dispose()
    }
}

function Save-Json {
    param([string]$Path, $Value)
    $json = $Value | ConvertTo-Json -Depth 8
    [System.IO.File]::WriteAllText($Path, $json, (New-Object System.Text.UTF8Encoding($false)))
}

$requestFile = $null
$pipelineExit = 1
$previousConsoleEncoding = [Console]::OutputEncoding
try {
    if ($PSVersionTable.PSVersion.Major -lt 5) { throw '需要 Windows PowerShell 5.1 或更新版本。' }
    $inputUri = Get-InputUri $Url
    $skillRoot = Split-Path -Parent $PSScriptRoot
    $pythonExe = Join-Path $skillRoot 'runtime\python\python.exe'
    $pipeline = Join-Path $PSScriptRoot 'text_pipeline.py'
    if (-not $CatalogOnly) {
        if (-not (Test-Path -LiteralPath $pythonExe -PathType Leaf)) { throw '随包运行环境缺失，请完整解压新版 ZIP；无需自行安装 Python。' }
        [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
        & $pythonExe -I -B $pipeline --check
        if ($LASTEXITCODE -ne 0) { throw '随包转写环境检查失败，未开始下载。' }
    }
    $accountId = Resolve-AccountId $inputUri
    $info = Get-AccountInfo $accountId
    $catalog = Get-PublicVideos $accountId $MaxPages
    $videos = @($catalog.Videos)
    $catalogComplete = $catalog.EndReached -and ($info.ExpectedCount -eq $videos.Count)
    if ([string]::IsNullOrWhiteSpace($OutputDirectory)) {
        $OutputDirectory = Join-Path ([Environment]::GetFolderPath('UserProfile')) 'Downloads\抖音文字素材'
    }
    $accountDir = Join-Path $OutputDirectory $accountId
    New-Item -ItemType Directory -Path $accountDir -Force | Out-Null
    $catalogItems = @($videos | ForEach-Object {
        [pscustomobject]@{ id = $_.Id; title = $_.Title; created_at = $_.CreatedAt; duration_ms = $_.DurationMs; expected_bytes = $_.ExpectedBytes }
    })
    Save-Json (Join-Path $accountDir 'catalog.json') ([pscustomobject]@{
        account_id = $accountId; account_name = $info.Name; expected_count = $info.ExpectedCount
        listed_count = $videos.Count; pages = $catalog.Pages; catalog_complete = $catalogComplete; items = $catalogItems
    })
    if ($CatalogOnly) {
        [pscustomobject]@{ status = 'catalog_only'; account_id = $accountId; account_name = $info.Name; listed_count = $videos.Count; catalog_complete = $catalogComplete; output_dir = $accountDir } | ConvertTo-Json -Compress
        $pipelineExit = if ($catalogComplete) { 0 } else { 2 }
    } else {
        $jobs = @($videos | Select-Object -First $MaxVideos | ForEach-Object {
            [pscustomobject]@{ id = $_.Id; title = $_.Title; expected_bytes = $_.ExpectedBytes; media_urls = @($_.MediaUrls) }
        })
        $requestFile = Join-Path $accountDir ('.request-' + [Guid]::NewGuid().ToString('N') + '.json')
        Save-Json $requestFile ([pscustomobject]@{
            account_id = $accountId; account_name = $info.Name; listed_count = $videos.Count
            catalog_complete = $catalogComplete; jobs = $jobs
        })
        $pipelineArgs = @('-I', '-B', $pipeline, '--request', $requestFile, '--output', $accountDir)
        if ($KeepVideos) { $pipelineArgs += '--keep-videos' }
        & $pythonExe @pipelineArgs
        $pipelineExit = $LASTEXITCODE
    }
} catch {
    [pscustomobject]@{ status = 'error'; message = $_.Exception.Message } | ConvertTo-Json -Compress
    $pipelineExit = 1
} finally {
    if ($requestFile -and (Test-Path -LiteralPath $requestFile -PathType Leaf)) { Remove-Item -LiteralPath $requestFile -Force }
    [Console]::OutputEncoding = $previousConsoleEncoding
}
exit $pipelineExit
