using System.Windows;
using System.Windows.Threading;
using CortenDesk.Remote.Services;
using CortenDesk.Remote.Views;

namespace CortenDesk.Remote;

public partial class App : Application
{
    /// <summary>
    /// Порядок запуска: настройки → окно входа (оно же чинит протухший токен)
    /// → главное окно. Shutdown явный, потому что первым показанным окном
    /// является окно входа, и закрывать приложение по нему нельзя.
    /// </summary>
    protected override void OnStartup(StartupEventArgs e)
    {
        base.OnStartup(e);

        // Необработанное исключение в UI-потоке иначе убивает приложение молча;
        // здесь оно хотя бы объясняется пользователю.
        DispatcherUnhandledException += OnUnhandled;

        var settings = AppSettings.Load();

        var login = new LoginWindow(settings);
        if (login.ShowDialog() != true || login.Client is null || login.User is null)
        {
            Shutdown();
            return;
        }

        var main = new MainWindow(login.Client, login.User, settings);
        MainWindow = main;
        main.Closed += (_, _) => Shutdown();
        main.Show();
    }

    private void OnUnhandled(object sender, DispatcherUnhandledExceptionEventArgs e)
    {
        MessageBox.Show(
            "Непредвиденная ошибка:\n\n" + e.Exception.Message,
            "CortenDesk Remote",
            MessageBoxButton.OK,
            MessageBoxImage.Error);

        e.Handled = true;
    }
}
