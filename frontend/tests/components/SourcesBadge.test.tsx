import { describe, it, expect } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import SourcesBadge from "@/components/SourcesBadge";
import { SourceCitation } from "@/types/chat";

const CITATION = "Section 1: Home Water Damage Coverage (sample_policy.md)";
const MESSAGE_ID = "m1";

/** Opens the collapsed list, which is the only path to the pills. */
function openSources(): void {
  fireEvent.click(screen.getAllByTestId("sources-toggle")[0]);
}

describe("SourcesBadge", () => {
  it("expands a pill that carries a retrieved excerpt", () => {
    render(
      <SourcesBadge
        messageId={MESSAGE_ID}
        sources={[
          {
            title: CITATION,
            excerpt: "Sudden pipe bursts are covered up to $25,000.",
          },
        ]}
      />,
    );

    // The list starts collapsed, so the pill is not in the document yet.
    expect(
      screen.queryByText("Sudden pipe bursts are covered up to $25,000."),
    ).not.toBeInTheDocument();

    openSources();

    fireEvent.click(
      screen.getByRole("button", { name: new RegExp(CITATION.slice(0, 12)) }),
    );

    expect(
      screen.getByText("Sudden pipe bursts are covered up to $25,000."),
    ).toBeInTheDocument();
  });

  it("keeps a pill with no excerpt inert but self-describing", () => {
    const sources: SourceCitation[] = [CITATION];
    render(<SourcesBadge sources={sources} messageId={MESSAGE_ID} />);

    openSources();

    const pill = screen.getByText(CITATION);
    // Not a button: there is nothing to open, so the pill must not invite a click.
    expect(pill.tagName).toBe("SPAN");
    expect(pill).toHaveAttribute("title", CITATION);
    expect(
      screen.queryByRole("button", { name: new RegExp(CITATION.slice(0, 12)) }),
    ).not.toBeInTheDocument();
  });

  it("renders nothing when there are no sources", () => {
    const { container } = render(
      <SourcesBadge sources={[]} messageId={MESSAGE_ID} />,
    );

    expect(container).toBeEmptyDOMElement();
  });

  it("keeps the count visible while the list is collapsed", () => {
    render(
      <SourcesBadge
        messageId={MESSAGE_ID}
        sources={[CITATION, "Section 4: Deductibles (sample_policy.md)"]}
      />,
    );

    expect(screen.getByTestId("sources-count")).toHaveTextContent("Sources (2)");
    expect(screen.getByTestId("sources-toggle")).toHaveAttribute(
      "aria-expanded",
      "false",
    );
    expect(screen.queryByTestId("sources-list")).not.toBeInTheDocument();

    openSources();

    expect(screen.getByTestId("sources-toggle")).toHaveAttribute(
      "aria-expanded",
      "true",
    );
    expect(screen.getByTestId("sources-list")).toBeInTheDocument();
  });

  it("namespaces its ids by message so two badges cannot point at each other", () => {
    render(
      <>
        <SourcesBadge messageId="m1" sources={[CITATION]} />
        <SourcesBadge messageId="m2" sources={[CITATION]} />
      </>,
    );

    const [firstToggle, secondToggle] = screen.getAllByTestId("sources-toggle");
    expect(firstToggle).toHaveAttribute("id", "sources-toggle-m1");
    expect(secondToggle).toHaveAttribute("id", "sources-toggle-m2");
    expect(firstToggle).toHaveAttribute("aria-controls", "sources-list-m1");
    expect(secondToggle).toHaveAttribute("aria-controls", "sources-list-m2");

    fireEvent.click(firstToggle);
    fireEvent.click(secondToggle);

    const [firstList, secondList] = screen.getAllByTestId("sources-list");
    expect(firstList).toHaveAttribute("id", "sources-list-m1");
    expect(secondList).toHaveAttribute("id", "sources-list-m2");
    // Each pill renders inside the list of the message that owns it.
    expect(firstList).toContainElement(screen.getAllByText(CITATION)[0]);
    expect(secondList).toContainElement(screen.getAllByText(CITATION)[1]);
  });
});
