import GreetingCore

#if canImport(SwiftUI)
  import SwiftUI

  @main
  struct GreetingApplication: App {
    var body: some Scene {
      WindowGroup {
        Text(Greeting.message(for: "SwiftUI"))
          .padding()
      }
    }
  }
#else
  @main
  struct GreetingCommandLine {
    static func main() {
      print(Greeting.message(for: "command line"))
    }
  }
#endif
