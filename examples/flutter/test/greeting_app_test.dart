import 'package:flutter_test/flutter_test.dart';
import 'package:flutter_workspace_example/main.dart';

void main() {
  testWidgets('shows the greeting', (tester) async {
    await tester.pumpWidget(const GreetingApp());

    expect(find.text('Hello from Flutter'), findsOneWidget);
  });
}
