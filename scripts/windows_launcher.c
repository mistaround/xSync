#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <wchar.h>

enum { PATH_CAPACITY = 32768 };

static void show_error(const wchar_t *message) {
    MessageBoxW(NULL, message, L"xSync", MB_OK | MB_ICONERROR);
}

int WINAPI wWinMain(HINSTANCE instance, HINSTANCE previous, PWSTR arguments, int show) {
    (void)instance;
    (void)previous;
    (void)show;
    static wchar_t directory[PATH_CAPACITY], python[PATH_CAPACITY], command[PATH_CAPACITY];
    DWORD length = GetModuleFileNameW(NULL, directory, PATH_CAPACITY);
    if (!length || length >= PATH_CAPACITY) {
        show_error(L"Could not locate the xSync installation.");
        return 1;
    }
    wchar_t *separator = wcsrchr(directory, L'\\');
    if (!separator) {
        show_error(L"Could not locate the xSync installation.");
        return 1;
    }
    *separator = L'\0';
    int n = swprintf(python, PATH_CAPACITY, L"%ls\\runtime\\pythonw.exe", directory);
    if (n < 0 || n >= PATH_CAPACITY) {
        show_error(L"The xSync installation path is too long.");
        return 1;
    }
    n = swprintf(command, PATH_CAPACITY, L"\"%ls\" \"%ls\\app\\run.py\" %ls",
                 python, directory, arguments);
    if (n < 0 || n >= PATH_CAPACITY) {
        show_error(L"The xSync launch command is too long.");
        return 1;
    }
    STARTUPINFOW startup = {0};
    PROCESS_INFORMATION process = {0};
    startup.cb = sizeof startup;
    if (!CreateProcessW(python, command, NULL, NULL, FALSE, 0, NULL, directory, &startup, &process)) {
        show_error(L"Could not start xSync. Extract the complete ZIP file and keep its folders with xSync.exe.");
        return 1;
    }
    CloseHandle(process.hThread);
    CloseHandle(process.hProcess);
    return 0;
}
