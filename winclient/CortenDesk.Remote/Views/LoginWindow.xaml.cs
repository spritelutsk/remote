using System.Net.Http;
using System.Windows;
using CortenDesk.Remote.Models;
using CortenDesk.Remote.Services;

namespace CortenDesk.Remote.Views;

/// <summary>
/// Вход в клиентский API CortenDesk.
///
/// Если с прошлого запуска остался токен, окно сначала молча проверяет его
/// через /api/currentUser и, если он жив, закрывается само — форму пользователь
/// в этом случае даже не увидит.
/// </summary>
public partial class LoginWindow : Window
{
    private readonly AppSettings _settings;

    public LoginWindow(AppSettings settings)
    {
        InitializeComponent();

        _settings = settings;

        ServerBox.Text = settings.ServerUrl;
        UserBox.Text = settings.Username;
        RememberBox.IsChecked = settings.RememberMe;

        Loaded += OnLoaded;
    }

    /// <summary>Готовый авторизованный клиент — забирает App после ShowDialog.</summary>
    public CortenDeskClient? Client { get; private set; }

    public UserPayload? User { get; private set; }

    private async void OnLoaded(object sender, RoutedEventArgs e)
    {
        var token = AppSettings.LoadToken();
        if (string.IsNullOrEmpty(token))
        {
            FocusFirstEmpty();
            return;
        }

        SetBusy(true, "Проверяем сохранённый вход…");

        CortenDeskClient? client = null;
        try
        {
            client = new CortenDeskClient(_settings.ServerUrl) { Token = token };
            var user = await client.CurrentUserAsync(CancellationToken.None);

            Client = client;
            User = user;
            DialogResult = true;
            return;
        }
        catch (Exception)
        {
            // Любая осечка на сохранённом токене — просто показываем форму.
            client?.Dispose();
            AppSettings.ClearToken();
        }

        SetBusy(false, "");
        FocusFirstEmpty();
    }

    private async void OnLoginClick(object sender, RoutedEventArgs e)
    {
        ErrorText.Visibility = Visibility.Collapsed;

        var server = ServerBox.Text.Trim();
        var username = UserBox.Text.Trim();
        var password = PassBox.Password;

        if (username.Length == 0 || password.Length == 0)
        {
            ShowError("Введите логин и пароль.");
            return;
        }

        SetBusy(true, "Подключаемся к серверу…");

        CortenDeskClient? client = null;
        try
        {
            client = new CortenDeskClient(server);

            var user = await client.LoginAsync(
                username, password, _settings.DeviceId, _settings.DeviceUuid, CancellationToken.None);

            _settings.ServerUrl = client.BaseUri.ToString().TrimEnd('/');
            _settings.Username = username;
            _settings.RememberMe = RememberBox.IsChecked == true;
            _settings.Save();

            if (_settings.RememberMe && client.Token is not null)
                AppSettings.SaveToken(client.Token);
            else
                AppSettings.ClearToken();

            Client = client;
            User = user;
            DialogResult = true;
        }
        catch (CortenDeskException ex)
        {
            client?.Dispose();
            SetBusy(false, "");
            ShowError(ex.Message);
        }
        catch (HttpRequestException ex)
        {
            client?.Dispose();
            SetBusy(false, "");
            ShowError("Не удалось связаться с сервером: " + ex.Message);
        }
        catch (TaskCanceledException)
        {
            client?.Dispose();
            SetBusy(false, "");
            ShowError("Сервер не ответил вовремя.");
        }
    }

    private void FocusFirstEmpty()
    {
        if (UserBox.Text.Length == 0) UserBox.Focus();
        else PassBox.Focus();
    }

    private void SetBusy(bool busy, string status)
    {
        LoginButton.IsEnabled = !busy;
        ServerBox.IsEnabled = !busy;
        UserBox.IsEnabled = !busy;
        PassBox.IsEnabled = !busy;
        StatusText.Text = status;
    }

    private void ShowError(string message)
    {
        ErrorText.Text = message;
        ErrorText.Visibility = Visibility.Visible;
    }
}
