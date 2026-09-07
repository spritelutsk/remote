using System.Collections.ObjectModel;
using System.ComponentModel;
using System.Diagnostics;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Data;
using System.Windows.Input;
using System.Windows.Threading;
using CortenDesk.Remote.Models;
using CortenDesk.Remote.Services;
using CortenDesk.Remote.Views;

namespace CortenDesk.Remote;

/// <summary>
/// Список устройств из /api/peers с присутствием, поиском и фильтрами.
/// Присутствие на сервере обновляется примерно раз в 15 секунд (heartbeat
/// клиента), поэтому и опрос идёт с тем же шагом — чаще смысла нет.
/// </summary>
public partial class MainWindow : Window
{
    private readonly CortenDeskClient _client;
    private readonly UserPayload _user;
    private readonly AppSettings _settings;

    private readonly ObservableCollection<PeerRow> _rows = new();
    private readonly ICollectionView _view;
    private readonly DispatcherTimer _timer;

    private readonly CancellationTokenSource _cts = new();
    private bool _ready;
    private bool _refreshing;
    private bool _closing;

    public MainWindow(CortenDeskClient client, UserPayload user, AppSettings settings)
    {
        InitializeComponent();

        _client = client;
        _user = user;
        _settings = settings;

        ServerLabel.Text = client.BaseUri.Host;
        UserLabel.Text = user.IsAdmin ? $"{user.Title} · администратор" : user.Title;
        OnlineOnlyBox.IsChecked = settings.OnlineOnly;

        _view = CollectionViewSource.GetDefaultView(_rows);
        _view.Filter = FilterRow;
        DeviceGrid.ItemsSource = _view;

        _timer = new DispatcherTimer
        {
            Interval = TimeSpan.FromSeconds(settings.AutoRefreshSeconds),
        };
        _timer.Tick += async (_, _) => await RefreshAsync(silent: true);

        _ready = true;

        Loaded += async (_, _) =>
        {
            await LoadGroupsAsync();
            await RefreshAsync(silent: false);
            _timer.Start();
        };
    }

    // ---- Загрузка данных ---------------------------------------------------

    private async Task LoadGroupsAsync()
    {
        // Комбобокс папок необязателен: если прав на него нет, просто оставляем
        // единственный пункт «Все папки» и не мешаем работать со списком.
        var items = new List<string> { "Все папки" };

        try
        {
            items.AddRange(await _client.GetDeviceGroupsAsync(_cts.Token));
        }
        catch (Exception)
        {
        }

        GroupBox.ItemsSource = items;
        GroupBox.SelectedIndex = 0;
    }

    private async Task RefreshAsync(bool silent)
    {
        if (_refreshing || _closing) return;
        _refreshing = true;

        if (!silent) StatusText.Text = "Загружаем список устройств…";

        try
        {
            var peers = await _client.GetPeersAsync(_cts.Token);

            // Полная перерисовка вместо диффа: список устройств здесь исчисляется
            // десятками, а выделение восстанавливается по ID.
            var selectedId = (DeviceGrid.SelectedItem as PeerRow)?.Id;

            _rows.Clear();
            foreach (var peer in peers.Select(p => new PeerRow(p)))
                _rows.Add(peer);

            _view.Refresh();

            if (selectedId is not null)
                DeviceGrid.SelectedItem = _rows.FirstOrDefault(r => r.Id == selectedId);

            var online = _rows.Count(r => r.IsOnline);
            StatusText.Text =
                $"Устройств: {_rows.Count} · в сети: {online} · обновлено в {DateTime.Now:HH:mm:ss}";
        }
        catch (AuthExpiredException)
        {
            _timer.Stop();
            AppSettings.ClearToken();

            MessageBox.Show(this,
                "Сессия на сервере истекла. Приложение закроется — войдите заново.",
                "CortenDesk Remote", MessageBoxButton.OK, MessageBoxImage.Warning);

            Close();
        }
        catch (OperationCanceledException)
        {
        }
        catch (Exception ex)
        {
            // Обрыв связи не должен ронять приложение и не должен глушить
            // автообновление: следующая попытка через интервал таймера.
            StatusText.Text = "Не удалось обновить список: " + ex.Message;
        }
        finally
        {
            _refreshing = false;
        }
    }

    // ---- Фильтрация --------------------------------------------------------

    private bool FilterRow(object item)
    {
        if (item is not PeerRow row) return false;

        if (OnlineOnlyBox.IsChecked == true && !row.IsOnline)
            return false;

        if (GroupBox.SelectedIndex > 0
            && !string.Equals(row.Group, GroupBox.SelectedItem as string, StringComparison.OrdinalIgnoreCase))
            return false;

        return row.Matches(SearchBox.Text);
    }

    private void OnFilterChanged(object sender, RoutedEventArgs e)
    {
        // Событие прилетает и на этапе InitializeComponent, когда полей ещё нет.
        if (!_ready) return;

        _view.Refresh();

        if (_settings.OnlineOnly != (OnlineOnlyBox.IsChecked == true))
        {
            _settings.OnlineOnly = OnlineOnlyBox.IsChecked == true;
            _settings.Save();
        }
    }

    // ---- Действия ----------------------------------------------------------

    private async void OnRefreshClick(object sender, RoutedEventArgs e) =>
        await RefreshAsync(silent: false);

    private void OnRowDoubleClick(object sender, MouseButtonEventArgs e)
    {
        // Двойной клик по заголовку или пустому месту не должен ничего открывать.
        if (DeviceGrid.SelectedItem is PeerRow row) Connect(row);
    }

    private void OnConnectClick(object sender, RoutedEventArgs e)
    {
        var row = RowFrom(sender);
        if (row is not null) Connect(row);
    }

    private void OnCopyIdClick(object sender, RoutedEventArgs e)
    {
        var row = RowFrom(sender);
        if (row is null) return;

        try
        {
            Clipboard.SetText(row.Id);
            StatusText.Text = $"ID {row.Id} скопирован в буфер обмена.";
        }
        catch (Exception)
        {
            // Буфер обмена бывает занят другим процессом — не повод падать.
            StatusText.Text = "Не удалось скопировать ID: буфер обмена занят.";
        }
    }

    private void OnOpenInBrowserClick(object sender, RoutedEventArgs e)
    {
        var row = RowFrom(sender);
        if (row is null) return;

        var url = _client.WebClientUri(row.Id).ToString();
        Process.Start(new ProcessStartInfo(url) { UseShellExecute = true });
    }

    private async void OnLogoutClick(object sender, RoutedEventArgs e)
    {
        var confirm = MessageBox.Show(this,
            "Выйти из учётной записи? Сохранённый вход будет удалён.",
            "CortenDesk Remote", MessageBoxButton.YesNo, MessageBoxImage.Question);

        if (confirm != MessageBoxResult.Yes) return;

        _timer.Stop();
        AppSettings.ClearToken();
        await _client.LogoutAsync(CancellationToken.None);
        Close();
    }

    private void Connect(PeerRow row)
    {
        if (!row.IsOnline)
        {
            var confirm = MessageBox.Show(this,
                $"Устройство «{row.DeviceName}» сейчас не в сети. Всё равно попробовать подключиться?",
                "CortenDesk Remote", MessageBoxButton.YesNo, MessageBoxImage.Question);

            if (confirm != MessageBoxResult.Yes) return;
        }

        var session = new SessionWindow(_client, row) { Owner = this };
        session.Show();
    }

    /// <summary>
    /// Строка, к которой относится действие: у кнопки в ячейке это её
    /// DataContext, у пункта контекстного меню — выделенная строка сетки.
    /// </summary>
    private PeerRow? RowFrom(object sender)
    {
        if (sender is FrameworkElement { DataContext: PeerRow row })
            return row;

        return DeviceGrid.SelectedItem as PeerRow;
    }

    protected override void OnClosing(CancelEventArgs e)
    {
        _closing = true;
        _timer.Stop();
        _cts.Cancel();
        _cts.Dispose();
        _client.Dispose();

        base.OnClosing(e);
    }
}
