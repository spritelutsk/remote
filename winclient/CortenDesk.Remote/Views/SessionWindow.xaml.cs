using System.Diagnostics;
using System.IO;
using System.Windows;
using System.Windows.Input;
using Microsoft.Web.WebView2.Core;
using CortenDesk.Remote.Models;
using CortenDesk.Remote.Services;

namespace CortenDesk.Remote.Views;

/// <summary>
/// Сеанс удалённого управления: нативный веб-клиент CortenDesk во встроенном
/// WebView2.
///
/// Здесь две разные авторизации, и это не случайность:
///   - список устройств берётся по Bearer клиентского API (см. CortenDeskClient);
///   - страница /webclient закрыта обычной сессией консоли (middleware `auth`),
///     а Bearer в неё не подставить.
/// Поэтому первый сеанс на новой машине просит войти прямо в окне — форма
/// консоли (или SSO-редирект на портал) открывается внутри WebView2. Cookie
/// живёт в собственной папке профиля, так что вход запоминается между запусками.
/// </summary>
public partial class SessionWindow : Window
{
    /// <summary>Сколько раз пытаться вернуться на /webclient после входа.</summary>
    private const int MaxResumeAttempts = 3;

    private readonly CortenDeskClient _client;
    private readonly PeerRow _peer;
    private readonly Uri _target;

    /// <summary>
    /// Origin-ы, куда этому окну позволено ходить.
    ///
    /// Одного адреса консоли мало: вход идёт через SSO на портал, а это другой
    /// хост, и жёсткий список сломал бы логин. Поэтому цель серверного
    /// редиректа с уже разрешённой страницы добавляется сюда — это и есть нога
    /// SSO. Скрипт на странице так сделать не может: он инициирует обычную
    /// навигацию, а она проверяется до того, как что-либо будет добавлено.
    /// </summary>
    private readonly HashSet<string> _allowedOrigins = new(StringComparer.OrdinalIgnoreCase);

    private int _resumeAttempts;
    private bool _fullscreen;
    private WindowState _savedState = WindowState.Normal;

    public SessionWindow(CortenDeskClient client, PeerRow peer)
    {
        InitializeComponent();

        _client = client;
        _peer = peer;
        _target = client.WebClientUri(peer.Id);

        Title = $"{peer.DeviceName} ({peer.Id}) — CortenDesk Remote";
        TitleText.Text = peer.DeviceName;
        StatusText.Text = peer.Id;

        if (TryGetOrigin(_target.ToString(), out var consoleOrigin))
            _allowedOrigins.Add(consoleOrigin);

        PreviewKeyDown += OnPreviewKeyDown;
        Loaded += OnLoaded;
    }

    private async void OnLoaded(object sender, RoutedEventArgs e)
    {
        // Профиль WebView2 общий для всех сеансов приложения: cookie сессии
        // консоли должна переживать закрытие окна, иначе вход спрашивался бы
        // при каждом подключении.
        var profile = Path.Combine(
            Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
            "CortenDeskRemote", "WebView2");

        try
        {
            Directory.CreateDirectory(profile);

            var environment = await CoreWebView2Environment.CreateAsync(userDataFolder: profile);
            await Web.EnsureCoreWebView2Async(environment);
        }
        catch (WebView2RuntimeNotFoundException)
        {
            ShowOverlay(
                "Не установлен WebView2 Runtime",
                "Встроенный веб-клиент работает на движке Microsoft Edge WebView2. "
                + "Он есть в Windows 11 и там, где установлен Edge; иначе его нужно поставить отдельно "
                + "(один раз, ставится тихо и не требует перезагрузки).",
                showDownload: true);
            return;
        }
        catch (Exception ex)
        {
            ShowOverlay("Не удалось запустить встроенный браузер", ex.Message, showDownload: false);
            return;
        }

        var core = Web.CoreWebView2;

        // Всплывающие окна веб-клиента (например, передача файлов) остаются
        // внутри этого же окна, иначе они уходят в системный браузер, где нет
        // нашей сессии. Адрес при этом обязан пройти проверку: раньше сюда
        // попадал любой Uri со страницы, включая file:// и сайт атакующего,
        // а окно живёт в профиле с cookie консоли.
        core.NewWindowRequested += (_, args) =>
        {
            args.Handled = true;
            if (IsAllowed(args.Uri)) core.Navigate(args.Uri);
        };

        // Вторая линия: сама навигация. Без неё страница просто присвоила бы
        // location и уехала на чужой origin мимо обработчика выше.
        core.NavigationStarting += (_, args) =>
        {
            if (IsAllowed(args.Uri)) return;

            // Редирект с уже разрешённой страницы — это шаг SSO, его цель тоже
            // становится разрешённой. Обычная навигация (IsRedirected == false)
            // так список расширить не может.
            if (args.IsRedirected && IsAllowed(core.Source) && TryGetOrigin(args.Uri, out var origin))
            {
                _allowedOrigins.Add(origin);
                return;
            }

            args.Cancel = true;
        };

        core.NavigationCompleted += OnNavigationCompleted;
        core.ProcessFailed += (_, _) =>
            ShowOverlay("Встроенный браузер аварийно завершился",
                "Нажмите «Переподключиться», чтобы открыть сеанс заново.", showDownload: false);

        core.Navigate(_target.ToString());
    }

    /// <summary>
    /// Разбор того, куда нас в итоге привёл сервер.
    ///
    /// Порядок ветвлений важен: сначала отсекается успех, затем внешний
    /// провайдер SSO (портал — чужой хост, туда лезть нельзя), затем формы
    /// входа консоли, и только оставшееся считается «вошли, но не туда» —
    /// это редирект на главную после логина, откуда мы сами возвращаемся
    /// на страницу веб-клиента.
    /// </summary>
    private void OnNavigationCompleted(object? sender, CoreWebView2NavigationCompletedEventArgs e)
    {
        var current = Web.CoreWebView2?.Source;
        if (string.IsNullOrEmpty(current) || !Uri.TryCreate(current, UriKind.Absolute, out var uri))
            return;

        if (uri.AbsolutePath.Equals("/webclient", StringComparison.OrdinalIgnoreCase))
        {
            _resumeAttempts = 0;
            HideOverlay();
            StatusText.Text = $"{_peer.Id} · сеанс открыт";
            return;
        }

        if (!uri.Host.Equals(_client.BaseUri.Host, StringComparison.OrdinalIgnoreCase))
        {
            // Портал-провайдер OIDC: ждём, пока он вернёт браузер обратно.
            HideOverlay();
            StatusText.Text = "Вход через единую учётную запись…";
            return;
        }

        if (IsAuthPage(uri.AbsolutePath))
        {
            HideOverlay();
            StatusText.Text = "Войдите в консоль, чтобы открыть сеанс";
            return;
        }

        if (_resumeAttempts++ < MaxResumeAttempts)
        {
            StatusText.Text = "Возвращаемся к устройству…";
            Web.CoreWebView2?.Navigate(_target.ToString());
            return;
        }

        ShowOverlay("Не удалось открыть веб-клиент",
            "Сервер увёл нас со страницы сеанса. Проверьте, что у вашей учётной записи "
            + $"есть доступ к устройству {_peer.Id}.", showDownload: false);
    }

    /// <summary>Схема http/https и origin из списка разрешённых.</summary>
    private bool IsAllowed(string? url) =>
        TryGetOrigin(url, out var origin) && _allowedOrigins.Contains(origin);

    /// <summary>Origin адреса; false для не-URL и для схем вроде file: и javascript:.</summary>
    private static bool TryGetOrigin(string? url, out string origin)
    {
        origin = "";

        if (string.IsNullOrEmpty(url) || !Uri.TryCreate(url, UriKind.Absolute, out var uri))
            return false;

        if (uri.Scheme != Uri.UriSchemeHttp && uri.Scheme != Uri.UriSchemeHttps)
            return false;

        origin = uri.GetLeftPart(UriPartial.Authority);
        return true;
    }

    private static bool IsAuthPage(string path) =>
        path.StartsWith("/login", StringComparison.OrdinalIgnoreCase)
        || path.StartsWith("/oidc", StringComparison.OrdinalIgnoreCase)
        || path.StartsWith("/invite", StringComparison.OrdinalIgnoreCase)
        || path.StartsWith("/forgot-password", StringComparison.OrdinalIgnoreCase)
        || path.StartsWith("/reset-password", StringComparison.OrdinalIgnoreCase);

    // ---- Кнопки ------------------------------------------------------------

    private void OnReconnectClick(object sender, RoutedEventArgs e)
    {
        _resumeAttempts = 0;
        HideOverlay();
        StatusText.Text = "Переподключаемся…";
        Web.CoreWebView2?.Navigate(_target.ToString());
    }

    private void OnOpenInBrowserClick(object sender, RoutedEventArgs e) =>
        Process.Start(new ProcessStartInfo(_target.ToString()) { UseShellExecute = true });

    private void OnDownloadRuntimeClick(object sender, RoutedEventArgs e) =>
        Process.Start(new ProcessStartInfo(
            "https://developer.microsoft.com/microsoft-edge/webview2/") { UseShellExecute = true });

    private void OnFullscreenClick(object sender, RoutedEventArgs e) => ToggleFullscreen();

    private void OnPreviewKeyDown(object sender, KeyEventArgs e)
    {
        // F11 — как в браузере; Esc выходит из полноэкранного режима, но не
        // закрывает окно: во время сеанса Esc нужен удалённой машине.
        if (e.Key == Key.F11)
        {
            ToggleFullscreen();
            e.Handled = true;
        }
        else if (e.Key == Key.Escape && _fullscreen)
        {
            ToggleFullscreen();
            e.Handled = true;
        }
    }

    private void ToggleFullscreen()
    {
        _fullscreen = !_fullscreen;

        if (_fullscreen)
        {
            _savedState = WindowState;
            Toolbar.Visibility = Visibility.Collapsed;
            WindowStyle = WindowStyle.None;
            ResizeMode = ResizeMode.NoResize;
            WindowState = WindowState.Normal;   // иначе Maximized не перерисуется
            WindowState = WindowState.Maximized;
        }
        else
        {
            Toolbar.Visibility = Visibility.Visible;
            WindowStyle = WindowStyle.SingleBorderWindow;
            ResizeMode = ResizeMode.CanResize;
            WindowState = _savedState;
        }
    }

    // ---- Заглушка ----------------------------------------------------------

    private void ShowOverlay(string title, string text, bool showDownload)
    {
        OverlayTitle.Text = title;
        OverlayText.Text = text;
        OverlayButton.Visibility = showDownload ? Visibility.Visible : Visibility.Collapsed;
        Overlay.Visibility = Visibility.Visible;
    }

    private void HideOverlay() => Overlay.Visibility = Visibility.Collapsed;

    protected override void OnClosed(EventArgs e)
    {
        Web.Dispose();
        base.OnClosed(e);
    }
}
