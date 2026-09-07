import GreetingCore
import XCTest

public final class GreetingTests: XCTestCase {
  func testUsesFallbackForBlankName() {
    XCTAssertEqual(Greeting.message(for: "  \n"), "Hello, friend!")
  }

  func testTrimsProvidedName() {
    XCTAssertEqual(Greeting.message(for: " Ada "), "Hello, Ada!")
  }
  public static let allTests = [
    ("testUsesFallbackForBlankName", testUsesFallbackForBlankName),
    ("testTrimsProvidedName", testTrimsProvidedName),
  ]
}
